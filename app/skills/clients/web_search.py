"""Web search skill clients: Tavily, Exa (env-configured)."""
from __future__ import annotations

from typing import Any

from ..base import SkillError, env_any, require_env
from .http_util import arequest_json


async def tavily_search(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    env = require_env("TAVILY_API_KEY")
    query = (args.get("query") or "").strip()
    if not query:
        raise SkillError("invalid_args", "missing_required:query")
    count = int(args.get("count") or 5)
    status, data = await arequest_json(
        "POST",
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {env['TAVILY_API_KEY']}"},
        body={
            "api_key": env["TAVILY_API_KEY"],
            "query": query,
            "max_results": min(max(count, 1), 10),
            "include_answer": False,
        },
        timeout=25.0,
    )
    if status != 200:
        raise SkillError("web_search_http_error", f"tavily_{status}:{data}")
    results = []
    for item in (data or {}).get("results") or []:
        results.append(
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": item.get("content") or item.get("snippet"),
            }
        )
    return {"provider": "tavily", "query": query, "count": len(results), "results": results}


async def exa_search(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    env = require_env("EXA_API_KEY")
    query = (args.get("query") or "").strip()
    if not query:
        raise SkillError("invalid_args", "missing_required:query")
    count = int(args.get("count") or 5)
    status, data = await arequest_json(
        "POST",
        "https://api.exa.ai/search",
        headers={"x-api-key": env["EXA_API_KEY"], "Content-Type": "application/json"},
        body={"query": query, "num_results": min(max(count, 1), 10)},
        timeout=25.0,
    )
    if status != 200:
        raise SkillError("web_search_http_error", f"exa_{status}:{data}")
    results = []
    for item in (data or {}).get("results") or []:
        results.append(
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": item.get("text") or item.get("snippet"),
            }
        )
    return {"provider": "exa", "query": query, "count": len(results), "results": results}


async def web_search(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Prefer Tavily, then Exa."""
    if env_any("TAVILY_API_KEY"):
        return await tavily_search(args, ctx)
    if env_any("EXA_API_KEY"):
        return await exa_search(args, ctx)
    raise SkillError("web_search_not_configured", "missing_env:TAVILY_API_KEY|EXA_API_KEY")
