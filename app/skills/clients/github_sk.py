"""GitHub skills — token from env; optional allowlist from GITHUB_ALLOWED_REPOSITORIES."""
from __future__ import annotations

import os
import re
from typing import Any

from ..base import SkillError, env_any
from .http_util import arequest_json

_REPO_RE = re.compile(r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)$")
_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)",
    re.I,
)


def _normalize_repo(value: str) -> str:
    value = (value or "").strip().removesuffix(".git")
    m = _URL_RE.search(value) or _REPO_RE.match(value)
    if not m:
        raise SkillError("invalid_github_repository", value[:120])
    return f"{m.group('owner')}/{m.group('repo')}"


def _allowed(repo: str) -> bool:
    raw = (os.environ.get("GITHUB_ALLOWED_REPOSITORIES") or "").strip()
    if not raw:
        return True  # open if unset; production should set allowlist
    allowed = {x.strip().lower() for x in raw.split(",") if x.strip()}
    return repo.lower() in allowed


def _token() -> str:
    pair = env_any("GITHUB_TOKEN", "GH_TOKEN")
    if not pair:
        raise SkillError("github_not_configured", "missing_env:GITHUB_TOKEN")
    return pair[1]


async def _gh(path: str, timeout: float = 20.0) -> Any:
    token = _token()
    status, data = await arequest_json(
        "GET",
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "AION-Skills",
        },
        timeout=timeout,
    )
    if status == 401:
        raise SkillError("github_auth_failed", "bad_credentials")
    if status == 404:
        raise SkillError("github_not_found", path)
    if status != 200:
        raise SkillError("github_http_error", f"{status}:{data}")
    return data


async def github_repo(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    repo = _normalize_repo(args.get("repository") or "")
    if not _allowed(repo):
        raise SkillError("github_repository_not_allowed", repo)
    data = await _gh(f"/repos/{repo}")
    return {
        "repository": repo,
        "description": data.get("description"),
        "default_branch": data.get("default_branch"),
        "stars": data.get("stargazers_count"),
        "forks": data.get("forks_count"),
        "open_issues": data.get("open_issues_count"),
        "language": data.get("language"),
        "license": (data.get("license") or {}).get("spdx_id"),
        "pushed_at": data.get("pushed_at"),
        "html_url": data.get("html_url"),
        "private": data.get("private"),
    }


async def github_file(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    import base64

    repo = _normalize_repo(args.get("repository") or "")
    path = (args.get("path") or "").lstrip("/")
    ref = (args.get("ref") or "").strip()
    if not path:
        raise SkillError("invalid_args", "missing_required:path")
    if not _allowed(repo):
        raise SkillError("github_repository_not_allowed", repo)
    q = f"?ref={ref}" if ref else ""
    data = await _gh(f"/repos/{repo}/contents/{path}{q}")
    content = data.get("content") or ""
    encoding = data.get("encoding")
    text = None
    if encoding == "base64" and content:
        text = base64.b64decode(content).decode("utf-8", errors="replace")
    return {
        "repository": repo,
        "path": path,
        "ref": ref or data.get("sha"),
        "size": data.get("size"),
        "text": (text or "")[:100_000],
        "html_url": data.get("html_url"),
    }


async def github_issues(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    repo = _normalize_repo(args.get("repository") or "")
    if not _allowed(repo):
        raise SkillError("github_repository_not_allowed", repo)
    data = await _gh(f"/repos/{repo}/issues?state=open&per_page=20")
    items = []
    for it in data or []:
        if "pull_request" in it:
            continue
        items.append(
            {
                "number": it.get("number"),
                "title": it.get("title"),
                "state": it.get("state"),
                "html_url": it.get("html_url"),
            }
        )
    return {"repository": repo, "count": len(items), "items": items}


async def github_search(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    repo = _normalize_repo(args.get("repository") or "")
    query = (args.get("query") or "").strip()
    if not query:
        raise SkillError("invalid_args", "missing_required:query")
    if not _allowed(repo):
        raise SkillError("github_repository_not_allowed", repo)
    q = f"{query} repo:{repo}"
    from urllib.parse import quote

    data = await _gh(f"/search/code?q={quote(q)}&per_page={min(int(args.get('limit') or 10), 20)}")
    items = [
        {
            "path": it.get("path"),
            "html_url": it.get("html_url"),
            "repository": repo,
        }
        for it in (data or {}).get("items") or []
    ]
    return {"repository": repo, "query": query, "count": len(items), "items": items}
