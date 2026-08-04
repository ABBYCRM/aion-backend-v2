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

    async def search(self, query: str, *, count: int | None = None, freshness: str | None = None) -> list[Any]:
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
        for item in raw:
            url = str(item.get("href") or item.get("url") or "")
            if not url.startswith(("https://", "http://")): continue
            results.append(WebResult(
                title=str(item.get("title") or url)[:300],
                url=url,
                snippet=str(item.get("body") or item.get("snippet") or "")[:1200],
                published_at=None,
            ))
        audit.record("tool.ddg_search", {"query_hash": _hash_text(query), "result_count": len(results)})
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

    async def search(self, query: str, *, count: int | None = None, freshness: str | None = None) -> list[Any]:
        from .tools import ToolConfigurationError, ToolRequestError
        # Try Brave if configured
        if settings_have_brave():
            try:
                return await self._brave.search(query, count=count, freshness=freshness)
            except ToolConfigurationError:
                pass  # no key, fall through to DDG
            except ToolRequestError as exc:
                # Brave returned an HTTP error — log and fall through to DDG
                logger.info("brave search failed (%s), trying DDG", exc)
        # Fall back to DuckDuckGo
        return await self._ddg.search(query, count=count, freshness=freshness)

    @staticmethod
    def as_context(results: list[Any]) -> str:
        from .tools import BraveSearch
        return BraveSearch.as_context(results)


def settings_have_brave() -> bool:
    from .settings import settings
    return bool(settings.brave_api_key)
