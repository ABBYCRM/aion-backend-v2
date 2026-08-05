"""Scrape / browser / screenshot skills — Firecrawl, ScrapingBee, Scrapfly, ScreenshotOne, Steel."""
from __future__ import annotations

from typing import Any

from ..base import SkillError, env_any, require_env
from .http_util import arequest_json


async def firecrawl_scrape(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    env = require_env("FIRECRAWL_API_KEY")
    url = (args.get("url") or "").strip()
    if not url.startswith("http"):
        raise SkillError("invalid_args", "url_must_be_http")
    status, data = await arequest_json(
        "POST",
        "https://api.firecrawl.dev/v1/scrape",
        headers={"Authorization": f"Bearer {env['FIRECRAWL_API_KEY']}"},
        body={"url": url, "formats": ["markdown"]},
        timeout=60.0,
    )
    if status not in (200, 201):
        raise SkillError("scrape_http_error", f"firecrawl_{status}")
    md = ((data or {}).get("data") or {}).get("markdown") or (data or {}).get("markdown") or ""
    return {"provider": "firecrawl", "url": url, "markdown": md[:50_000]}


async def scrapingbee_scrape(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from urllib.parse import urlencode

    env = require_env("SCRAPINGBEE_API_KEY")
    url = (args.get("url") or "").strip()
    if not url.startswith("http"):
        raise SkillError("invalid_args", "url_must_be_http")
    q = urlencode({"api_key": env["SCRAPINGBEE_API_KEY"], "url": url, "render_js": "false"})
    status, data = await arequest_json(
        "GET",
        f"https://app.scrapingbee.com/api/v1/?{q}",
        timeout=60.0,
    )
    if status != 200:
        raise SkillError("scrape_http_error", f"scrapingbee_{status}")
    text = data.get("text") if isinstance(data, dict) else str(data)
    return {"provider": "scrapingbee", "url": url, "text": (text or "")[:50_000]}


async def scrape_url(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    if env_any("FIRECRAWL_API_KEY"):
        return await firecrawl_scrape(args, ctx)
    if env_any("SCRAPINGBEE_API_KEY"):
        return await scrapingbee_scrape(args, ctx)
    if env_any("SCRAPFLY_API_KEY"):
        return await scrapfly_scrape(args, ctx)
    raise SkillError("scrape_not_configured", "missing_env:FIRECRAWL_API_KEY|SCRAPINGBEE_API_KEY|SCRAPFLY_API_KEY")


async def scrapfly_scrape(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from urllib.parse import urlencode

    env = require_env("SCRAPFLY_API_KEY")
    url = (args.get("url") or "").strip()
    if not url.startswith("http"):
        raise SkillError("invalid_args", "url_must_be_http")
    q = urlencode({"key": env["SCRAPFLY_API_KEY"], "url": url, "format": "json"})
    status, data = await arequest_json(
        "GET",
        f"https://api.scrapfly.io/scrape?{q}",
        timeout=60.0,
    )
    if status != 200:
        raise SkillError("scrape_http_error", f"scrapfly_{status}")
    result = (data or {}).get("result") or {}
    content = result.get("content") or ""
    return {"provider": "scrapfly", "url": url, "content": content[:50_000]}
