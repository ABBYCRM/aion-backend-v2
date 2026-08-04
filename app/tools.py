"""Server-side web search and allowlisted GitHub integrations."""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from .audit import audit
from .settings import settings

_REPOSITORY_RE = re.compile(r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)$")
_GITHUB_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)(?:/.*)?",
    re.I,
)
_BRANCH_RE = re.compile(r"^(?!/|.*(?:\.\.|//|@\{|\\|\.$))[A-Za-z0-9._/-]{1,200}$")


class ToolConfigurationError(RuntimeError):
    pass


class ToolRequestError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class WebResult:
    title: str
    url: str
    snippet: str
    published_at: str | None = None


class BraveSearch:
    async def search(
        self,
        query: str,
        *,
        count: int | None = None,
        freshness: str | None = None,
    ) -> list[WebResult]:
        if not settings.brave_api_key:
            raise ToolConfigurationError("web_search_not_configured")
        query = " ".join(query.split())[:400]
        if not query:
            raise ToolRequestError("empty_search_query")
        params: dict[str, Any] = {
            "q": query,
            "count": min(count or settings.web_search_max_results, settings.web_search_max_results, 20),
            "safesearch": "moderate",
            "extra_snippets": "true",
        }
        if freshness in {"pd", "pw", "pm", "py"}:
            params["freshness"] = freshness
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": settings.brave_api_key,
            "User-Agent": f"AION/{settings.app_version}",
        }
        try:
            async with httpx.AsyncClient(
                timeout=settings.request_timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.get(settings.brave_base_url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            audit.record("tool.web_search_failed", {"error_type": type(exc).__name__})
            raise ToolRequestError("web_search_unavailable", status_code=503) from exc
        if response.status_code != 200:
            audit.record("tool.web_search_failed", {"status": response.status_code})
            status = 429 if response.status_code == 429 else 503 if response.status_code >= 500 else 400
            raise ToolRequestError(f"web_search_http_{response.status_code}", status_code=status)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ToolRequestError("web_search_invalid_response", status_code=503) from exc
        results: list[WebResult] = []
        for item in (payload.get("web") or {}).get("results", [])[: params["count"]]:
            url = str(item.get("url") or "")
            if not url.startswith(("https://", "http://")):
                continue
            snippets = [str(item.get("description") or "")]
            snippets.extend(str(value) for value in item.get("extra_snippets") or [])
            results.append(
                WebResult(
                    title=str(item.get("title") or url)[:300],
                    url=url,
                    snippet=" ".join(part.strip() for part in snippets if part.strip())[:1_200],
                    published_at=item.get("age") or item.get("page_age"),
                )
            )
        audit.record("tool.web_search", {"query_hash": _hash_text(query), "result_count": len(results)})
        return results

    @staticmethod
    def as_context(results: list[WebResult]) -> str:
        if not results:
            return ""
        lines = ['<tool_results type="web_search" format="jsonl" untrusted="true">']
        used = len(lines[0])
        for index, result in enumerate(results, 1):
            encoded = _safe_json(
                {
                    "index": index,
                    "title": result.title,
                    "url": result.url,
                    "snippet": result.snippet,
                    "published_at": result.published_at,
                }
            )
            if used + len(encoded) + 32 > settings.max_tool_context_chars:
                break
            lines.append(encoded)
            used += len(encoded) + 1
        lines.append("</tool_results>")
        return "\n".join(lines) if len(lines) > 2 else ""


class GitHubClient:
    def __init__(self) -> None:
        self._cached_token = ""
        self._token_expiry = 0.0
        self._token_lock = asyncio.Lock()

    async def _token(self) -> str:
        if settings.github_app_configured:
            return await self._installation_token()
        if settings.github_token and settings.allow_github_token_fallback:
            return settings.github_token
        raise ToolConfigurationError("github_not_configured")

    async def _installation_token(self) -> str:
        async with self._token_lock:
            if self._cached_token and time.time() < self._token_expiry - 120:
                return self._cached_token
            import jwt

            now = int(time.time())
            app_jwt = jwt.encode(
                {"iat": now - 60, "exp": now + 540, "iss": settings.github_app_id},
                settings.github_private_key,
                algorithm="RS256",
            )
            url = (
                f"{settings.github_api_url}/app/installations/"
                f"{settings.github_installation_id}/access_tokens"
            )
            repository_names = sorted({repo.split("/", 1)[1] for repo in settings.github_allowed_repositories})
            body = {"repositories": repository_names}
            try:
                async with httpx.AsyncClient(
                    timeout=settings.request_timeout_seconds,
                    follow_redirects=False,
                ) as client:
                    response = await client.post(url, headers=self._headers(str(app_jwt)), json=body)
            except httpx.HTTPError as exc:
                raise ToolRequestError("github_token_unavailable", status_code=503) from exc
            if response.status_code != 201:
                audit.record("tool.github_token_failed", {"status": response.status_code})
                raise ToolRequestError(
                    f"github_token_http_{response.status_code}",
                    status_code=503,
                )
            payload = response.json()
            self._cached_token = str(payload.get("token") or "")
            if not self._cached_token:
                raise ToolRequestError("github_token_missing", status_code=503)
            expires_at = str(payload.get("expires_at") or "")
            try:
                self._token_expiry = datetime.fromisoformat(
                    expires_at.replace("Z", "+00:00")
                ).astimezone(timezone.utc).timestamp()
            except ValueError:
                self._token_expiry = time.time() + 3_300
            return self._cached_token

    @staticmethod
    def parse_repository(value: str) -> str:
        value = value.strip().removesuffix(".git")
        match = _GITHUB_URL_RE.search(value) or _REPOSITORY_RE.fullmatch(value)
        if not match:
            raise ToolRequestError("invalid_github_repository")
        repository = f"{match.group('owner')}/{match.group('repo')}"
        if not settings.repository_allowed(repository):
            raise ToolRequestError("github_repository_not_allowed", status_code=403)
        return repository

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        token = await self._token()
        url = f"{settings.github_api_url}{path}"
        try:
            async with httpx.AsyncClient(
                timeout=settings.request_timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.request(
                    method,
                    url,
                    headers=self._headers(token),
                    json=json_body,
                    params=params,
                )
        except httpx.HTTPError as exc:
            raise ToolRequestError("github_unavailable", status_code=503) from exc
        if response.status_code >= 400:
            audit.record(
                "tool.github_failed",
                {"method": method, "path": path, "status": response.status_code},
            )
            status = 429 if response.status_code == 429 else 503 if response.status_code >= 500 else response.status_code
            raise ToolRequestError(f"github_http_{response.status_code}", status_code=status)
        return None if response.status_code == 204 else response.json()

    async def get_repository(self, repository: str) -> dict[str, Any]:
        repository = self.parse_repository(repository)
        payload = await self.request("GET", f"/repos/{repository}")
        return {
            key: payload.get(key)
            for key in (
                "full_name", "description", "private", "default_branch", "archived",
                "language", "updated_at", "open_issues_count",
            )
        }

    async def get_file(self, repository: str, path: str, ref: str | None = None) -> dict[str, Any]:
        repository = self.parse_repository(repository)
        clean_path = _clean_path(path)
        payload = await self.request(
            "GET",
            f"/repos/{repository}/contents/{quote(clean_path, safe='/')}",
            params={"ref": ref} if ref else None,
        )
        if isinstance(payload, list):
            return {
                "repository": repository,
                "path": clean_path,
                "entries": [item.get("path") for item in payload[:100]],
            }
        content = ""
        if payload.get("encoding") == "base64" and payload.get("content"):
            content = base64.b64decode(payload["content"], validate=False).decode(
                "utf-8", errors="replace"
            )[:50_000]
        return {
            "repository": repository,
            "path": payload.get("path"),
            "sha": payload.get("sha"),
            "size": payload.get("size"),
            "content": content,
        }

    async def list_issues(
        self,
        repository: str,
        *,
        state: str = "open",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        repository = self.parse_repository(repository)
        payload = await self.request(
            "GET",
            f"/repos/{repository}/issues",
            params={"state": state, "per_page": min(max(limit, 1), 50)},
        )
        return [
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "state": item.get("state"),
                "is_pull_request": "pull_request" in item,
                "updated_at": item.get("updated_at"),
                "url": item.get("html_url"),
            }
            for item in payload
        ]

    async def search_code(
        self,
        repository: str,
        query: str,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        repository = self.parse_repository(repository)
        query = " ".join(query.split())[:200]
        if not query:
            raise ToolRequestError("empty_github_search_query")
        payload = await self.request(
            "GET",
            "/search/code",
            params={"q": f"{query} repo:{repository}", "per_page": min(max(limit, 1), 30)},
        )
        return [
            {
                "name": item.get("name"),
                "path": item.get("path"),
                "sha": item.get("sha"),
                "url": item.get("html_url"),
            }
            for item in payload.get("items", [])
        ]

    async def create_issue(self, repository: str, title: str, body: str) -> dict[str, Any]:
        self._require_write()
        repository = self.parse_repository(repository)
        payload = await self.request(
            "POST",
            f"/repos/{repository}/issues",
            json_body={"title": title[:256], "body": body[:60_000]},
        )
        return {
            "number": payload.get("number"),
            "url": payload.get("html_url"),
            "title": payload.get("title"),
        }

    async def create_branch(self, repository: str, branch: str, base: str = "main") -> dict[str, Any]:
        self._require_write()
        repository = self.parse_repository(repository)
        branch = _clean_branch(branch)
        base = _clean_branch(base)
        base_ref = await self.request(
            "GET", f"/repos/{repository}/git/ref/heads/{quote(base, safe='')}"
        )
        sha = base_ref["object"]["sha"]
        payload = await self.request(
            "POST",
            f"/repos/{repository}/git/refs",
            json_body={"ref": f"refs/heads/{branch}", "sha": sha},
        )
        return {"branch": branch, "sha": payload["object"]["sha"]}

    async def upsert_file(
        self,
        repository: str,
        path: str,
        content: str,
        message: str,
        branch: str,
    ) -> dict[str, Any]:
        self._require_write()
        repository = self.parse_repository(repository)
        clean_path = _clean_path(path)
        branch = _clean_branch(branch)
        existing_sha = None
        try:
            existing = await self.request(
                "GET",
                f"/repos/{repository}/contents/{quote(clean_path, safe='/')}",
                params={"ref": branch},
            )
            if isinstance(existing, dict):
                existing_sha = existing.get("sha")
        except ToolRequestError as exc:
            if str(exc) != "github_http_404":
                raise
        body: dict[str, Any] = {
            "message": message[:250],
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if existing_sha:
            body["sha"] = existing_sha
        payload = await self.request(
            "PUT",
            f"/repos/{repository}/contents/{quote(clean_path, safe='/')}",
            json_body=body,
        )
        return {"path": clean_path, "commit_sha": payload.get("commit", {}).get("sha")}

    async def create_pull_request(
        self,
        repository: str,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> dict[str, Any]:
        self._require_write()
        repository = self.parse_repository(repository)
        head = _clean_branch(head)
        base = _clean_branch(base)
        payload = await self.request(
            "POST",
            f"/repos/{repository}/pulls",
            json_body={
                "title": title[:256],
                "body": body[:60_000],
                "head": head,
                "base": base,
                "draft": True,
            },
        )
        return {
            "number": payload.get("number"),
            "url": payload.get("html_url"),
            "title": payload.get("title"),
        }

    def _require_write(self) -> None:
        if not settings.github_write_enabled:
            raise ToolConfigurationError("github_writes_disabled")

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": settings.github_api_version,
            "User-Agent": f"AION/{settings.app_version}",
        }

    @staticmethod
    def as_context(kind: str, repository: str, payload: Any) -> str:
        encoded = _safe_json(payload)
        encoded = encoded[: settings.max_tool_context_chars]
        return (
            f'<tool_results type="github_{kind}" repository="{repository}" '
            f'format="json" untrusted="true">\n{encoded}\n</tool_results>'
        )


def _clean_path(path: str) -> str:
    clean = path.strip().lstrip("/")
    parts = clean.split("/")
    if not clean or any(part in {"", ".", ".."} for part in parts) or "\\" in clean:
        raise ToolRequestError("invalid_github_path")
    return clean


def _clean_branch(value: str) -> str:
    branch = value.strip()
    if not _BRANCH_RE.fullmatch(branch):
        raise ToolRequestError("invalid_github_branch")
    return branch


def _safe_json(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _hash_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()[:16]


web_search = BraveSearch()
github = GitHubClient()
