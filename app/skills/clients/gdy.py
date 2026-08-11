"""GDY World client — async wrapper for the GDY tool/category/search APIs.

GDY (gdyworld.com) exposes a single token-gated REST surface that bundles
842+ tools, 25 categories, a search endpoint, and RAG scopes. It is
wired into AION as a meta-catalog — the agent searches the GDY index
when the user asks for "an agent that does X" instead of us hard-coding
specific tools.

API surface used (base = https://gdyworld.com/v1):
  GET /me            — account + scope check
  GET /tools?category=<id>&page=<n>&perPage=<n>
                     — paginated tool list, filter by category id
  GET /categories    — full category tree with tool counts
  GET /search?q=<q>&limit=<n>
                     — semantic / keyword search (response uses the
                       same {data: [...]} envelope as /tools)

Authentication:
  The token is read from vault key GDY_API_KEY (never from chat). A
  single shared token has scopes:
    tools:read, categories:read, search, rag:context, rag:snapshot
  See account-info response in app/skills/seed_all.py docstring.

Failure modes (all non-fatal — AION DEFERs on tool failure):
  401/403  → GdyAuthError (operator action: rotate key)
  429      → GdyRateLimitError (backoff + retry once)
  5xx      → GdyUpstreamError (retry with exponential backoff, max 2)
  network  → GdyNetworkError

This module uses the stdlib `http_util.request_json` (urllib) instead
of httpx so the skill runs in any Python env, not just the DO image
that has httpx installed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

GDY_BASE_URL = "https://gdyworld.com/v1"
GDY_TIMEOUT_SECONDS = 12.0
GDY_MAX_RETRIES = 2
GDY_BACKOFF_BASE = 0.6  # 0.6s, 1.2s


class GdyError(Exception):
    """Base for all GDY client errors. Always safe to surface to AION."""
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code or self.__class__.__name__


class GdyAuthError(GdyError):
    """Token missing, invalid, or revoked. Operator action: rotate."""

class GdyRateLimitError(GdyError):
    """GDY said 429. Backoff + retry once."""

class GdyUpstreamError(GdyError):
    """5xx from GDY. Retry with backoff, then DEFER."""

class GdyNetworkError(GdyError):
    """DNS/TCP/timeout. Retry once."""


def _sync_request(method: str, url: str, *, headers: dict, body: dict | None, timeout: float) -> tuple[int, Any]:
    """Sync wrapper around http_util. Run in a thread for async compat."""
    # Imported lazily so the import chain stays light.
    from .http_util import request_json
    return request_json(method, url, headers=headers, body=body, timeout=timeout)


class GdyClient:
    """Thin async wrapper. One client per process is enough."""

    def __init__(self, token: str | None = None, *, base_url: str = GDY_BASE_URL) -> None:
        self._token = token or os.environ.get("GDY_API_KEY", "").strip()
        if not self._token:
            # Don't crash at import time — AION is fail-closed, so a
            # missing key only breaks the GDY skill, never the boot.
            self._token = ""
        self._base = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": "AION-GDYClient/1.0",
        }

    async def _request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> dict[str, Any]:
        if not self._token:
            raise GdyAuthError("GDY_API_KEY not configured (set it in vault or DO env vars)")
        url = f"{self._base}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        last_exc: GdyError | None = None
        for attempt in range(GDY_MAX_RETRIES + 1):
            try:
                status, payload = await asyncio.to_thread(
                    _sync_request, method, url, headers=self._headers(),
                    body=json_body, timeout=GDY_TIMEOUT_SECONDS,
                )
            except Exception as exc:  # network / timeout
                last_exc = GdyNetworkError(f"GDY network error: {exc}")
                if attempt < GDY_MAX_RETRIES:
                    await asyncio.sleep(GDY_BACKOFF_BASE * (2 ** attempt))
                    continue
                raise last_exc from exc
            if status in (401, 403):
                raise GdyAuthError(
                    f"GDY auth failed ({status}): rotate GDY_API_KEY in vault",
                    status=status,
                )
            if status == 429:
                last_exc = GdyRateLimitError("GDY rate limited (429)", status=429)
                if attempt < GDY_MAX_RETRIES:
                    await asyncio.sleep(GDY_BACKOFF_BASE * (2 ** attempt))
                    continue
                raise last_exc
            if 500 <= status < 600:
                last_exc = GdyUpstreamError(
                    f"GDY upstream {status}",
                    status=status,
                )
                if attempt < GDY_MAX_RETRIES:
                    await asyncio.sleep(GDY_BACKOFF_BASE * (2 ** attempt))
                    continue
                raise last_exc
            if not isinstance(payload, dict):
                # Some GDY endpoints can return an empty body or a plain string.
                # Wrap so callers can always do `payload.get("data", [])`.
                payload = {"data": payload} if payload is not None else {}
            return payload
        # Unreachable but defensive.
        assert last_exc is not None
        raise last_exc

    # ----------------------------------------------------------------
    # Public API methods — one per AION skill executor
    # ----------------------------------------------------------------

    async def me(self) -> dict[str, Any]:
        """Account + scope check. Returns the full /v1/me response."""
        return await self._request("GET", "/me")

    async def list_categories(self) -> list[dict[str, Any]]:
        """Return all 25 GDY categories with tool counts."""
        payload = await self._request("GET", "/categories")
        return list(payload.get("data", []))

    async def list_tools(
        self,
        *,
        category: str | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> dict[str, Any]:
        """List tools. category is the category id (e.g. "18" for AI / Coding Agents)."""
        params: dict[str, Any] = {"page": page, "perPage": per_page}
        if category is not None:
            params["category"] = category
        return await self._request("GET", "/tools", params=params)

    async def search(self, query: str, *, limit: int = 20) -> dict[str, Any]:
        """Semantic / keyword search of the GDY index.

        GDY's actual search endpoint is GET /v1/search?q=<query> and the
        response uses the {data: [...]} envelope (same as /tools).
        Earlier we hit POST /v1/search which returns 401 with the
        user-search scope missing, so we use the verified path here.
        """
        return await self._request("GET", "/search", params={"q": query, "limit": limit})


# ----------------------------------------------------------------
# Module-level cache — categories don't change often, cache 5 min.
# ----------------------------------------------------------------
_CATEGORIES_CACHE: dict[str, Any] = {}
_CATEGORIES_CACHED_AT: float = 0.0


async def cached_categories(client: GdyClient) -> list[dict[str, Any]]:
    """Return categories, refreshing from GDY at most every 5 min."""
    global _CATEGORIES_CACHED_AT, _CATEGORIES_CACHE
    now = time.monotonic()
    if _CATEGORIES_CACHE and (now - _CATEGORIES_CACHED_AT) < 300.0:
        return list(_CATEGORIES_CACHE.values())
    cats = await client.list_categories()
    _CATEGORIES_CACHE = {c.get("id"): c for c in cats}
    _CATEGORIES_CACHED_AT = now
    return cats


# ----------------------------------------------------------------
# AION skill executors — wired in seed_all.py via wire_executors()
# ----------------------------------------------------------------

async def gdy_me(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Skill: gdy.me — return the GDY account + scopes for this AION key."""
    try:
        client = GdyClient()
        payload = await client.me()
        scopes = list(payload.get("token", {}).get("scopes", []))
        return {
            "ok": True,
            "skill_id": "gdy.me",
            "account": payload,
            "scopes": scopes,
        }
    except GdyError as exc:
        return {"ok": False, "skill_id": "gdy.me", "error_code": exc.code, "error_message": str(exc)}


async def gdy_categories(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Skill: gdy.categories — list all 25 GDY categories with tool counts."""
    try:
        client = GdyClient()
        cats = await cached_categories(client)
        total = sum(int(c.get("toolCount", 0)) for c in cats)
        return {
            "ok": True,
            "skill_id": "gdy.categories",
            "total_categories": len(cats),
            "total_tools": total,
            "categories": cats,
        }
    except GdyError as exc:
        return {"ok": False, "skill_id": "gdy.categories", "error_code": exc.code, "error_message": str(exc)}


async def gdy_tools(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Skill: gdy.tools — list tools, filter by category."""
    try:
        category = (args.get("category") or "").strip() or None
        page = max(1, int(args.get("page") or 1))
        per_page = max(1, min(100, int(args.get("per_page") or 25)))
        client = GdyClient()
        payload = await client.list_tools(category=category, page=page, per_page=per_page)
        tools = list(payload.get("data", []))
        return {
            "ok": True,
            "skill_id": "gdy.tools",
            "total": payload.get("total", 0),
            "page": payload.get("page", page),
            "perPage": payload.get("perPage", per_page),
            "hasMore": payload.get("hasMore", False),
            "tools": tools,
        }
    except GdyError as exc:
        return {"ok": False, "skill_id": "gdy.tools", "error_code": exc.code, "error_message": str(exc)}


async def gdy_search(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Skill: gdy.search — semantic / keyword search of the GDY index."""
    query = (args.get("query") or "").strip()
    if not query:
        return {"ok": False, "skill_id": "gdy.search",
                "error_code": "missing_required:query",
                "error_message": "query is required"}
    try:
        limit = max(1, min(100, int(args.get("limit") or 20)))
        client = GdyClient()
        payload = await client.search(query, limit=limit)
        return {
            "ok": True,
            "skill_id": "gdy.search",
            "query": query,
            "hits": list(payload.get("data", payload.get("hits", []))),
        }
    except GdyError as exc:
        return {"ok": False, "skill_id": "gdy.search", "error_code": exc.code, "error_message": str(exc)}


# ===========================================================================
# v2.8.12 — local meta-catalog backfill
# ===========================================================================
# GDY's live index has only ~17 of the 70+ repos from the operator's
# ai-coding-rag-skills-github-directory.md doc. When the agent is asked
# "what's the best X for Y", and X is not in GDY (e.g. context7,
# superpowers, claude-context, docling, haystack, qdrant), the live
# /v1/search?q=X returns 0 hits. To keep the agent useful, we ship a
# curated data/gdy_meta_catalog.json with the full directory + best_for
# and section, and expose it via gdy.meta_catalog_search. The agent
# hits live GDY first, then falls back to the local catalog with a
# "from_local" flag so the user knows where the data came from.
import json as _json
from pathlib import Path as _Path

# Find the repo root by walking up from this file until we find the
# data/ directory next to us. This avoids off-by-one path bugs.
_META_CATALOG_PATH: _Path | None = None
for _candidate in (_Path(__file__).resolve().parent, *_Path(__file__).resolve().parents):
    if (_candidate / "data" / "gdy_meta_catalog.json").is_file():
        _META_CATALOG_PATH = _candidate / "data" / "gdy_meta_catalog.json"
        break
if _META_CATALOG_PATH is None:
    # Fallback: assume CWD-relative
    _META_CATALOG_PATH = _Path("data/gdy_meta_catalog.json").resolve()
_META_CATALOG_CACHE: list[dict] | None = None


def _load_meta_catalog() -> list[dict]:
    global _META_CATALOG_CACHE
    if _META_CATALOG_CACHE is not None:
        return _META_CATALOG_CACHE
    if not _META_CATALOG_PATH.exists():
        return []
    try:
        _META_CATALOG_CACHE = _json.loads(_META_CATALOG_PATH.read_text())
    except Exception:
        _META_CATALOG_CACHE = []
    return _META_CATALOG_CACHE


async def gdy_meta_catalog_search(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Skill: gdy.meta_catalog_search — search the local AION-curated
    meta-catalog (data/gdy_meta_catalog.json) of 70+ AI coding / RAG /
    MCP / agent-framework repos. Used as a fallback when live GDY
    search returns 0 hits. The local catalog includes best_for,
    section, mcp, primary_language, and in_gdy flags.

    Inputs:
      query (required): free-text term to match against name, repo, best_for, section
      limit (optional, default 10): max hits to return
      section (optional): filter by directory section name (e.g. "AI coding agents")
    """
    query = (args.get("query") or "").strip()
    if not query:
        return {"ok": False, "skill_id": "gdy.meta_catalog_search",
                "error_code": "missing_required:query",
                "error_message": "query is required"}
    try:
        limit = max(1, min(50, int(args.get("limit") or 10)))
    except (TypeError, ValueError):
        limit = 10
    section = (args.get("section") or "").strip().lower()
    catalog = _load_meta_catalog()
    if not catalog:
        return {"ok": False, "skill_id": "gdy.meta_catalog_search",
                "error_code": "catalog_not_loaded",
                "error_message": "data/gdy_meta_catalog.json not found or empty"}
    ql = query.lower()
    hits: list[dict[str, Any]] = []
    for entry in catalog:
        if section and entry.get("section", "").lower() != section:
            continue
        # Score: name match > repo match > best_for match > section match
        score = 0
        if ql in entry.get("name", "").lower():
            score += 10
        if ql in entry.get("repo", "").lower():
            score += 6
        for token in ql.split():
            if token in entry.get("best_for", "").lower():
                score += 2
            if token in entry.get("section", "").lower():
                score += 1
        if score > 0:
            hits.append({"score": score, **entry})
    hits.sort(key=lambda h: -h["score"])
    return {
        "ok": True,
        "skill_id": "gdy.meta_catalog_search",
        "query": query,
        "source": "data/gdy_meta_catalog.json",
        "from_local": True,
        "total": len(hits),
        "hits": hits[:limit],
    }
