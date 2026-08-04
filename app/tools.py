"""Server-side web search and GitHub integrations."""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .audit import audit
from .settings import settings

_REPOSITORY_RE = re.compile(r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)$")
_GITHUB_URL_RE = re.compile(r"https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)(?:/.*)?", re.I)

class ToolConfigurationError(RuntimeError): pass
class ToolRequestError(RuntimeError): pass

@dataclass(frozen=True)
class WebResult:
    title: str
    url: str
    snippet: str
    published_at: str | None = None

class BraveSearch:
    async def search(self, query: str, *, count: int | None = None, freshness: str | None = None) -> list[WebResult]:
        if not settings.brave_api_key: raise ToolConfigurationError("web_search_not_configured")
        query = " ".join(query.split())[:400]
        if not query: raise ToolRequestError("empty_search_query")
        params: dict[str, Any] = {"q": query, "count": min(count or settings.web_search_max_results, settings.web_search_max_results, 20), "safesearch": "moderate", "extra_snippets": "true"}
        if freshness in {"pd", "pw", "pm", "py"}: params["freshness"] = freshness
        headers = {"Accept": "application/json", "X-Subscription-Token": settings.brave_api_key, "User-Agent": f"AION/{settings.app_version}"}
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds, follow_redirects=False) as client: response = await client.get(settings.brave_base_url, params=params, headers=headers)
        if response.status_code != 200:
            audit.record("tool.web_search_failed", {"status": response.status_code}); raise ToolRequestError(f"web_search_http_{response.status_code}")
        payload = response.json(); results = []
        for item in (payload.get("web") or {}).get("results", [])[:params["count"]]:
            url = str(item.get("url") or "")
            if not url.startswith(("https://", "http://")): continue
            snippets = [str(item.get("description") or "")]; snippets.extend(str(value) for value in item.get("extra_snippets") or [])
            results.append(WebResult(title=str(item.get("title") or url)[:300], url=url, snippet=" ".join(part.strip() for part in snippets if part.strip())[:1200], published_at=item.get("age") or item.get("page_age")))
        audit.record("tool.web_search", {"query_hash": _hash_text(query), "result_count": len(results)}); return results

    @staticmethod
    def as_context(results: list[WebResult]) -> str:
        if not results: return ""
        lines = ["<tool_results type=\"web_search\" untrusted=\"true\">"]
        for index, result in enumerate(results, 1):
            published = f"; published={result.published_at}" if result.published_at else ""
            lines.append(f"[{index}] {result.title}\nURL: {result.url}{published}\nSnippet: {result.snippet}")
        lines.append("</tool_results>"); return "\n\n".join(lines)

class GitHubClient:
    def __init__(self) -> None:
        self._cached_token = ""; self._token_expiry = 0.0; self._token_lock = asyncio.Lock()

    async def _token(self) -> str:
        if settings.github_token: return settings.github_token
        if not settings.github_app_configured: raise ToolConfigurationError("github_not_configured")
        async with self._token_lock:
            if self._cached_token and time.time() < self._token_expiry - 120: return self._cached_token
            import jwt
            now = int(time.time()); app_jwt = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": settings.github_app_id}, settings.github_private_key, algorithm="RS256")
            url = f"{settings.github_api_url}/app/installations/{settings.github_installation_id}/access_tokens"
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds, follow_redirects=False) as client: response = await client.post(url, headers=self._headers(str(app_jwt)), json={})
            if response.status_code != 201: audit.record("tool.github_token_failed", {"status": response.status_code}); raise ToolRequestError(f"github_token_http_{response.status_code}")
            self._cached_token = str(response.json()["token"]); self._token_expiry = time.time() + 3300; return self._cached_token

    @staticmethod
    def parse_repository(value: str) -> str:
        value = value.strip().removesuffix(".git"); match = _GITHUB_URL_RE.search(value) or _REPOSITORY_RE.match(value)
        if not match: raise ToolRequestError("invalid_github_repository")
        repository = f"{match.group('owner')}/{match.group('repo')}"
        if not settings.repository_allowed(repository): raise ToolRequestError("github_repository_not_allowed")
        return repository

    async def request(self, method: str, path: str, *, json_body: dict | None = None, params: dict | None = None) -> Any:
        token = await self._token(); url = f"{settings.github_api_url}{path}"
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds, follow_redirects=False) as client: response = await client.request(method, url, headers=self._headers(token), json=json_body, params=params)
        if response.status_code >= 400: audit.record("tool.github_failed", {"method": method, "path": path, "status": response.status_code}); raise ToolRequestError(f"github_http_{response.status_code}")
        return None if response.status_code == 204 else response.json()

    async def get_repository(self, repository: str) -> dict[str, Any]:
        repository = self.parse_repository(repository); payload = await self.request("GET", f"/repos/{repository}")
        return {key: payload.get(key) for key in ("full_name", "description", "private", "default_branch", "archived", "language", "updated_at", "open_issues_count")}

    async def get_file(self, repository: str, path: str, ref: str | None = None) -> dict[str, Any]:
        repository = self.parse_repository(repository); clean_path = path.strip().lstrip("/")
        if not clean_path or ".." in clean_path.split("/"): raise ToolRequestError("invalid_github_path")
        payload = await self.request("GET", f"/repos/{repository}/contents/{quote(clean_path, safe='/')}", params={"ref": ref} if ref else None)
        if isinstance(payload, list): return {"repository": repository, "path": clean_path, "entries": [item.get("path") for item in payload[:100]]}
        content = ""
        if payload.get("encoding") == "base64" and payload.get("content"): content = base64.b64decode(payload["content"], validate=False).decode("utf-8", errors="replace")[:100_000]
        return {"repository": repository, "path": payload.get("path"), "sha": payload.get("sha"), "size": payload.get("size"), "content": content}

    async def list_issues(self, repository: str, *, state: str = "open", limit: int = 20) -> list[dict[str, Any]]:
        repository = self.parse_repository(repository); payload = await self.request("GET", f"/repos/{repository}/issues", params={"state": state, "per_page": min(max(limit, 1), 50)})
        return [{"number": item.get("number"), "title": item.get("title"), "state": item.get("state"), "is_pull_request": "pull_request" in item, "updated_at": item.get("updated_at"), "url": item.get("html_url")} for item in payload]

    async def search_code(self, repository: str, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        repository = self.parse_repository(repository); query = " ".join(query.split())[:200]
        if not query: raise ToolRequestError("empty_github_search_query")
        payload = await self.request("GET", "/search/code", params={"q": f"{query} repo:{repository}", "per_page": min(max(limit, 1), 30)})
        return [{"name": item.get("name"), "path": item.get("path"), "sha": item.get("sha"), "url": item.get("html_url")} for item in payload.get("items", [])]

    async def create_issue(self, repository: str, title: str, body: str) -> dict[str, Any]:
        self._require_write(); repository = self.parse_repository(repository); payload = await self.request("POST", f"/repos/{repository}/issues", json_body={"title": title[:256], "body": body[:60000]}); return {"number": payload.get("number"), "url": payload.get("html_url"), "title": payload.get("title")}

    async def create_branch(self, repository: str, branch: str, base: str = "main") -> dict[str, Any]:
        self._require_write(); repository = self.parse_repository(repository); base_ref = await self.request("GET", f"/repos/{repository}/git/ref/heads/{quote(base, safe='')}"); sha = base_ref["object"]["sha"]
        payload = await self.request("POST", f"/repos/{repository}/git/refs", json_body={"ref": f"refs/heads/{branch}", "sha": sha}); return {"branch": branch, "sha": payload["object"]["sha"]}

    async def upsert_file(self, repository: str, path: str, content: str, message: str, branch: str) -> dict[str, Any]:
        self._require_write(); repository = self.parse_repository(repository); clean_path = path.strip().lstrip("/"); existing_sha = None
        try: existing_sha = (await self.request("GET", f"/repos/{repository}/contents/{quote(clean_path, safe='/')}", params={"ref": branch})).get("sha")
        except ToolRequestError as exc:
            if str(exc) != "github_http_404": raise
        body = {"message": message[:250], "content": base64.b64encode(content.encode()).decode(), "branch": branch}
        if existing_sha: body["sha"] = existing_sha
        payload = await self.request("PUT", f"/repos/{repository}/contents/{quote(clean_path, safe='/')}", json_body=body); return {"path": clean_path, "commit_sha": payload.get("commit", {}).get("sha")}

    async def create_pull_request(self, repository: str, title: str, body: str, head: str, base: str = "main") -> dict[str, Any]:
        self._require_write(); repository = self.parse_repository(repository); payload = await self.request("POST", f"/repos/{repository}/pulls", json_body={"title": title[:256], "body": body[:60000], "head": head, "base": base, "draft": True}); return {"number": payload.get("number"), "url": payload.get("html_url"), "title": payload.get("title")}

    def _require_write(self) -> None:
        if not settings.github_write_enabled: raise ToolConfigurationError("github_writes_disabled")

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2026-03-10", "User-Agent": f"AION/{settings.app_version}"}

    @staticmethod
    def as_context(kind: str, repository: str, payload: Any) -> str:
        return f"<tool_results type=\"github_{kind}\" repository=\"{repository}\" untrusted=\"true\">\n{json.dumps(payload, ensure_ascii=False, indent=2)[:100000]}\n</tool_results>"


def _hash_text(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.encode()).hexdigest()[:16]

from .search_ddg import DuckDuckGoSearch, ChainedWebSearch
web_search = ChainedWebSearch(brave=BraveSearch(), ddg=DuckDuckGoSearch()); github = GitHubClient()
