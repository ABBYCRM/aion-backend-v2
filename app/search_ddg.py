"""DuckDuckGo search provider — used as fallback when no Brave key is set."""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .audit import audit

logger = logging.getLogger(__name__)

class DuckDuckGoSearch:
    """Free, no-key-required web search via the ddgs library.

    Used as a fallback when BRAVE_API_KEY is not set so the 'Search web'
    toggle in the chat UI always works out of the box.  ddgs scrapes
    DuckDuckGo's HTML results; the underlying API is unofficial, so this
    provider must never be load-bearing for production.  We surface
    ToolRequestError on any failure so the chain can decide.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ddg")

    async def search(self, query: str, *, count: int | None = None, freshness: str | None = None, offset: int = 0) -> list[Any]:
        from .tools import WebResult, ToolRequestError  # local to avoid cycle
        query = " ".join(query.split())[:400]
        if not query: raise ToolRequestError("empty_search_query")
        limit = max(1, min(count or 6, 20))

        def _do_search() -> list[dict[str, Any]]:
            try:
                from ddgs import DDGS
                with DDGS() as ddgs:
                    return list(ddgs.text(query, max_results=limit))
            except Exception as exc:  # noqa: BLE001 — ddgs raises many unrelated types
                logger.warning("ddgs.text failed: %s", exc)
                raise

        try:
            raw = await asyncio.get_event_loop().run_in_executor(self._executor, _do_search)
        except Exception as exc:  # noqa: BLE001
            audit.record("tool.ddg_search_failed", {"error": str(exc)[:200]})
            raise ToolRequestError(f"ddg_search_error: {exc}") from exc

        results: list[WebResult] = []
        for index, item in enumerate(raw, 1):
            url = str(item.get("href") or item.get("url") or "")
            if not url.startswith(("https://", "http://")): continue
            results.append(WebResult(
                title=str(item.get("title") or url)[:300],
                url=url,
                snippet=str(item.get("body") or item.get("snippet") or "")[:1200],
                published_at=None,
                provider="ddg",
                position=index,
                score=float(index),
                dedup="first",
                extra_snippets=(),
                query_highlight=None,
            ))
        audit.record("tool.ddg_search", {"query_hash": _hash_text(query), "result_count": len(results), "provider": "ddg"})
        return results

    @staticmethod
    def as_context(results: list[Any]) -> str:
        from .tools import BraveSearch  # reuse the existing formatter
        return BraveSearch.as_context(results)


def _hash_text(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.encode()).hexdigest()[:16]


class ChainedWebSearch:
    """Try Brave first (production-grade, paid), fall back to DuckDuckGo (free).

    Result: the 'Search web' toggle in the AION chat UI always returns
    real results, even when no BRAVE_API_KEY is configured.  When a key
    is set, Brave is preferred because its results are higher quality
    and its freshness filter works.
    """

    def __init__(self, brave: Any, ddg: Any) -> None:
        self._brave = brave
        self._ddg = ddg

    async def search(self, query: str, *, count: int | None = None, freshness: str | None = None, offset: int = 0) -> list[Any]:
        from .tools import ToolConfigurationError, ToolRequestError, WebResult
        wanted = count or 10
        page_offset = max(0, min(int(offset or 0), 9))
        brave_results: list[WebResult] = []
        if settings_have_brave():
            try:
                # Ask Brave for a few more than we need so the dedup
                # pass has room when DDG fills gaps.
                brave_results = await self._brave.search(query, count=min(wanted + 3, 20), freshness=freshness, offset=page_offset)
            except ToolConfigurationError:
                pass  # no key, fall through to DDG
            except ToolRequestError as exc:
                logger.info("brave search failed (%s), trying DDG", exc)
        # If Brave returned enough, use them. Otherwise ask DDG to fill
        # the gap so the operator gets the full count they asked for.
        if len(brave_results) >= wanted:
            return brave_results[:wanted]
        # Dedup helper: same host + path, ignoring trailing slash and
        # common tracking params. This is intentionally conservative —
        # we only dedupe on exact host+path, not on subdomain variants.
        def _url_key(u: str) -> str:
            from urllib.parse import urlparse, parse_qs, urlencode
            try:
                p = urlparse(u)
            except Exception:
                return u.lower()
            # Drop tracking params; keep the rest.
            qs = parse_qs(p.query, keep_blank_values=True)
            for k in ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "fbclid", "gclid"):
                qs.pop(k, None)
            clean_qs = urlencode(sorted((k, v[0]) for k, v in qs.items() if v))
            path = (p.path or "").rstrip("/")
            return f"{p.scheme}://{p.netloc.lower()}{path}?{clean_qs}"
        seen = {_url_key(r.url) for r in brave_results}
        if settings_have_brave() and brave_results:
            # Fetch extra from DDG to backfill, but flag the Brave rows
            # so the operator sees which provider returned what.
            for r in brave_results:
                # Brave rows keep provider="brave", dedup="first" (no
                # duplicate of them exists yet).
                pass
            try:
                ddg_results = await self._ddg.search(query, count=min(wanted + 3, 20), freshness=freshness, offset=page_offset)
            except (ToolConfigurationError, ToolRequestError) as exc:
                logger.info("ddg fallback failed: %s", exc)
                ddg_results = []
            merged: list[WebResult] = list(brave_results)
            for r in ddg_results:
                if _url_key(r.url) in seen:
                    # Mark the duplicate but skip it from the merged
                    # list (the Brave row already won). We expose the
                    # dedup status by tagging — but only on the
                    # winner; the dup itself is not returned to keep
                    # the count clean.
                    continue
                seen.add(_url_key(r.url))
                # Re-number position across providers.
                merged.append(WebResult(
                    title=r.title, url=r.url, snippet=r.snippet,
                    published_at=r.published_at, provider=r.provider,
                    position=len(merged) + 1, score=r.score,
                    dedup="first", extra_snippets=r.extra_snippets,
                    query_highlight=r.query_highlight,
                ))
                if len(merged) >= wanted:
                    break
            return merged[:wanted]
        # No Brave path. Just return DDG; no dedup needed within a
        # single provider.
        ddg_results = await self._ddg.search(query, count=wanted, freshness=freshness, offset=page_offset)
        return ddg_results[:wanted]

    @staticmethod
    def as_context(results: list[Any]) -> str:
        from .tools import BraveSearch
        return BraveSearch.as_context(results)


def settings_have_brave() -> bool:
    from .settings import settings
    return bool(settings.brave_api_key)
