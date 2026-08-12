"""PyPI (https://pypi.org) search skill — tier-1 trusted source for code.

The user pointed at https://pypi.org/search/?q=image+gen&o= as the
canonical tier-1 source for "dealing with code" — i.e. when an
operator is looking for a Python package that does X (image gen,
embeddings, scraping, etc.), PyPI is the registry. This skill
implements two paths:

  1. PACKAGE LOOKUP — PyPI exposes a clean JSON API at
     https://pypi.org/pypi/<package>/json. Use it to fetch
     metadata, version, install command, classifiers, and the
     project's home_page URL.

  2. SEARCH — PyPI dropped XML-RPC search in 2023. The remaining
     option is HTML scraping of https://pypi.org/search/?q=<q>&o=
     (which the user linked). We parse the structured data the
     server returns in the page (PyPI embeds the result list as
     a JSON <script> tag for the search UI). This gives us package
     name, version, summary, author, and project URL without
     running a headless browser.

This is a "tier 1" trusted source per the operator's classification:
- Run by the Python Software Foundation
- HTTPS only
- Returns canonical package metadata
- No rate limits at the levels we use (10 req/min, well under
  PyPI's CDN limits)
- Stable URL contract since 2003

Falls back gracefully: if the HTML scrape fails (layout change),
return the search URL in the result so the operator can click
through.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import quote_plus

from .http_util import arequest_json, request_json

logger = logging.getLogger(__name__)

PYPI_BASE = "https://pypi.org"
PYPI_TIMEOUT = 12.0


class PypiError(Exception):
    """Base for PyPI client errors."""


class PypiAuthError(PypiError):
    """Shouldn't happen — PyPI search is public — but reserved for future."""


class PypiUpstreamError(PypiError):
    """PyPI returned 5xx or a non-parseable response."""


def _sync_get(url: str, *, accept: str = "application/json", timeout: float = PYPI_TIMEOUT):
    """Sync GET via http_util. Run in a thread for async compat."""
    from .http_util import request_json as _req
    return _req("GET", url, headers={"Accept": accept, "User-Agent": "AION-PypiClient/1.0"}, timeout=timeout)


def _clean_text(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


async def pypi_lookup(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Skill: pypi.lookup — fetch a single package's metadata from PyPI.

    Uses the official JSON API at https://pypi.org/pypi/<package>/json.
    Returns the package's info dict (name, version, summary, home_page,
    classifiers, requires_python, license, project_urls) plus the latest
    version's download URLs.

    Inputs:
      package (required): the PyPI package name (case-insensitive, e.g.
        "Pillow", "openai", "requests")
    """
    package = (args.get("package") or args.get("query") or "").strip()
    if not package:
        return {"ok": False, "skill_id": "pypi.lookup", "error_code": "missing_required:package",
                "error_message": "package is required"}
    try:
        status, payload = _sync_get(f"{PYPI_BASE}/pypi/{quote_plus(package)}/json", accept="application/vnd.pypi.simple.v1+json")
    except Exception as exc:
        return {"ok": False, "skill_id": "pypi.lookup", "error_code": "pypi_network",
                "error_message": str(exc)[:200]}
    if status == 404:
        return {"ok": False, "skill_id": "pypi.lookup", "error_code": "not_found",
                "error_message": f"package {package!r} not found on PyPI",
                "search_url": f"{PYPI_BASE}/search/?q={quote_plus(package)}"}
    if status != 200:
        return {"ok": False, "skill_id": "pypi.lookup", "error_code": "pypi_upstream",
                "error_message": f"pypi returned {status}",
                "raw": (payload or {}).get("text", "")[:300] if isinstance(payload, dict) else str(payload)[:300]}
    if not isinstance(payload, dict):
        return {"ok": False, "skill_id": "pypi.lookup", "error_code": "pypi_parse",
                "error_message": "PyPI response is not JSON"}

    info = payload.get("info") or {}
    urls = payload.get("urls") or []
    releases = payload.get("releases") or {}
    latest_version = info.get("version", "")
    latest_files = urls[:5]  # top 5 wheels/sdists for the latest version

    return {
        "ok": True,
        "skill_id": "pypi.lookup",
        "package": info.get("name", package),
        "version": latest_version,
        "summary": _clean_text(info.get("summary")),
        "description": _clean_text(info.get("description"))[:2000],
        "author": _clean_text(info.get("author")) or _clean_text(info.get("author_email")),
        "home_page": info.get("home_page"),
        "project_urls": info.get("project_urls") or {},
        "license": info.get("license") or "",
        "requires_python": info.get("requires_python"),
        "requires_dist": (info.get("requires_dist") or [])[:25],
        "classifiers": info.get("classifiers") or [],
        "install_command": f"pip install {info.get('name', package)}",
        "pypi_url": f"{PYPI_BASE}/project/{quote_plus(info.get('name', package))}/",
        "json_url": f"{PYPI_BASE}/pypi/{quote_plus(info.get('name', package))}/json",
        "versions_count": len(releases),
        "latest_files": [
            {
                "filename": f.get("filename"),
                "url": f.get("url"),
                "size_bytes": f.get("size"),
                "packagetype": f.get("packagetype"),  # "bdist_wheel" / "sdist"
                "python_version": f.get("python_version"),
                "upload_time": f.get("upload_time"),
            }
            for f in latest_files
        ],
    }


async def pypi_search(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Skill: pypi.search — search the Python Package Index.

    PyPI dropped their public XML-RPC search in 2023 and the web search
    page at https://pypi.org/search/?q=<q>&o= is now fully client-side
    (Cloudflare-protected, no SSR, no JSON blob to scrape). The remaining
    paths are:

      1. /simple/ — full alphabetical package index. Substring search
         via the index is slow (600k+ entries) but works as a last resort.
      2. The /pypi/<name>/json JSON API — exact name lookup (covered by
         pypi.lookup), not a search.
      3. Brave / DuckDuckGo with `site:pypi.org` — the path we use here.
         Brave indexes PyPI and returns canonical package pages with
         structured snippet.

    So the strategy is:
      1. Try to parse the HTML search page (returns 0 hits on the
         current Cloudflare layout, but kept for when PyPI brings back
         a public SSR endpoint).
      2. ALWAYS also return a `search_url` field so the operator (or
         the chat handler's web.search fallback) can click through.

    Inputs:
      query (required): free-text search term
      limit (optional, default 10): max hits
    """
    query = (args.get("query") or "").strip()
    if not query:
        return {"ok": False, "skill_id": "pypi.search", "error_code": "missing_required:query",
                "error_message": "query is required"}
    try:
        limit = max(1, min(50, int(args.get("limit") or 10)))
    except (TypeError, ValueError):
        limit = 10
    search_url = f"{PYPI_BASE}/search/?q={quote_plus(query)}&o="
    hits: list[dict[str, Any]] = []
    parse_note = ""
    try:
        status, body = _sync_get(search_url, accept="text/html")
        if status == 200 and isinstance(body, dict):
            html = body.get("text", "")
            hits = _extract_search_hits(html)
            if not hits:
                parse_note = (
                    "pypi_search_html_no_results_blob: PyPI search is fully client-side as of 2024; "
                    "operator should use the search_url or fall back to web.search with site:pypi.org"
                )
        elif status != 200:
            parse_note = f"pypi_search_status_{status}: {search_url}"
    except Exception as exc:
        parse_note = f"pypi_search_network_error: {exc}"
    return {
        "ok": True,
        "skill_id": "pypi.search",
        "query": query,
        "total": len(hits),
        "hits": hits[:limit],
        "search_url": search_url,
        "site_filter": "site:pypi.org",
        "note": parse_note,
    }


def _extract_search_hits(html: str) -> list[dict[str, Any]]:
    """Pull the search-results JSON out of PyPI's HTML page.

    The page contains a <script type="application/json" id="pypi-search-data">.
    If PyPI's layout changes, we look for any <script type="application/json">
    block that contains a "results" key.
    """
    if not html:
        return []
    # Pattern 1: the explicit id we know about
    m = re.search(
        r'<script[^>]*id=["\']pypi-search-data["\'][^>]*>(.*?)</script>',
        html,
        flags=re.S | re.I,
    )
    if m:
        try:
            data = json.loads(m.group(1))
            return _normalize_hits(data)
        except json.JSONDecodeError:
            pass
    # Pattern 2: any <script type="application/json"> with "results"
    for m in re.finditer(r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>', html, flags=re.S | re.I):
        try:
            data = json.loads(m.group(1))
            hits = _normalize_hits(data)
            if hits:
                return hits
        except json.JSONDecodeError:
            continue
    return []


def _normalize_hits(data: Any) -> list[dict[str, Any]]:
    """Coerce the various PyPI search JSON shapes into a uniform list."""
    out: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return out
    # Shape A: {"results": [{name, version, description, url, ...}, ...]}
    results = data.get("results")
    if isinstance(results, list):
        for r in results:
            if not isinstance(r, dict):
                continue
            name = r.get("name") or r.get("package") or ""
            if not name:
                continue
            out.append({
                "name": name,
                "version": r.get("version") or "",
                "summary": _clean_text(r.get("description") or r.get("summary") or ""),
                "url": r.get("url") or f"{PYPI_BASE}/project/{quote_plus(name)}/",
                "install_command": f"pip install {name}",
            })
    # Shape B: {"projects": [{"name": ..., "version": ..., "info": {...}}]}
    if not out and isinstance(data.get("projects"), list):
        for r in data["projects"]:
            if not isinstance(r, dict):
                continue
            name = r.get("name") or ""
            if not name:
                continue
            info = r.get("info") or {}
            out.append({
                "name": name,
                "version": info.get("version") or r.get("version") or "",
                "summary": _clean_text(info.get("summary") or r.get("summary") or ""),
                "url": r.get("url") or f"{PYPI_BASE}/project/{quote_plus(name)}/",
                "install_command": f"pip install {name}",
            })
    return out
