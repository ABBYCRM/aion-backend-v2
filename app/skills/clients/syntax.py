"""
AION syntax examples client — 900,000 code snippets across 9 technologies.

Operator pack: 100k_syntax_core_9 (9 .txt files, one per
technology, 100,000 syntax examples each).

Format (per line, after 3 comment lines):
    ID<TAB>construct<TAB>JSON-escaped snippet
Example:
    000001<TAB>for loop<TAB>"for i in range(10):\\n    print(i)"

The snippet is JSON-escaped so multiline code, quotes, tabs, and
pipe characters remain intact when we split on tab.

Design mirrors extra_scenarios.py:
  * Lazy load per technology (~7-13MB per file).
  * Per-technology layout: {by_id, ordered, total, path}.
  * Pre-decoded snippets stored in cache so consumers do not pay
    the json.loads cost on every read.

Skills (registered in seed_all.py, 3 contracts):
  - syntax.list    : return the 9-technology catalog + counts
  - syntax.get     : fetch one syntax row by (technology, id)
  - syntax.browse  : paginate; optional construct filter (substring)
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("aion.skills.syntax")

DATA_DIR_NAME = "syntax"

# technology -> (slug, display)
TECHNOLOGIES: dict[str, str] = {
    "python": "Python", "javascript": "JavaScript", "typescript": "TypeScript",
    "tailwind_css": "Tailwind CSS", "c": "C", "cplusplus": "C++",
    "java": "Java", "css": "CSS", "html": "HTML",
}

# Process-wide cache: technology -> {by_id, ordered, total, path}
_CACHE: dict[str, dict[str, Any]] = {}


def _data_roots() -> list[Path]:
    """Search paths for the syntax directory.

    Order: $AION_SYNTAX_DIR, $AION_DATA_DIR, package-relative, cwd,
    /app/data, /app/data/syntax.
    """
    roots: list[Path] = []
    for key in ("AION_SYNTAX_DIR", "AION_DATA_DIR"):
        v = os.environ.get(key)
        if v:
            roots.append(Path(v) / DATA_DIR_NAME)
            roots.append(Path(v))
    here = Path(__file__).resolve()
    roots.extend(
        [
            here.parents[3] / "data" / DATA_DIR_NAME,  # repo root
            here.parents[2] / "data" / DATA_DIR_NAME,
            Path.cwd() / "data" / DATA_DIR_NAME,
            Path.cwd() / "data",
            Path("/app/data") / DATA_DIR_NAME,
            Path("/app/data"),
        ]
    )
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        s = str(r)
        if s not in seen:
            seen.add(s)
            out.append(r)
    return out


def resolve_data_dir() -> Path:
    for root in _data_roots():
        if root.is_dir() and any(root.glob("*_100k_syntax.txt")):
            return root
    raise FileNotFoundError(
        "syntax data dir not found; copy the 9 *_100k_syntax.txt files "
        "into data/syntax/ or set $AION_SYNTAX_DIR."
    )


def list_technologies() -> list[dict[str, Any]]:
    """Return the 9-technology catalog (no file load)."""
    data_dir = resolve_data_dir()
    out: list[dict[str, Any]] = []
    for slug, display in TECHNOLOGIES.items():
        path = data_dir / f"{slug}_100k_syntax.txt"
        if path.is_file():
            # Count non-comment lines = 1 record each.
            n = 0
            with path.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    n += 1
            out.append(
                {
                    "technology": slug,
                    "display": display,
                    "file": path.name,
                    "count": n,
                    "size_bytes": path.stat().st_size,
                }
            )
    out.sort(key=lambda r: r["technology"])
    return out


def _load_technology(technology: str) -> dict[str, Any]:
    """Lazy-load one technology file into a {by_id, ordered} dict.
    Pre-decodes the JSON-escaped snippet so consumers do not pay
    the json.loads cost on every read."""
    technology = (technology or "").strip().lower()
    if technology not in TECHNOLOGIES:
        raise FileNotFoundError(f"unknown_technology:{technology}")
    if technology in _CACHE:
        return _CACHE[technology]
    data_dir = resolve_data_dir()
    path = data_dir / f"{technology}_100k_syntax.txt"
    if not path.is_file():
        raise FileNotFoundError(f"missing file: {path}")
    by_id: dict[str, dict[str, str]] = {}
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            sid, construct, encoded = parts
            try:
                snippet = json.loads(encoded)
            except json.JSONDecodeError:
                continue
            by_id[sid] = {
                "id": sid,
                "technology": technology,
                "display": TECHNOLOGIES[technology],
                "construct": construct,
                "snippet": snippet,
            }
    _CACHE[technology] = {
        "by_id": by_id,
        "ordered": list(by_id.values()),
        "total": len(by_id),
        "path": str(path),
    }
    return _CACHE[technology]


# ===========================================================================
# Skill entry points
# ===========================================================================

async def syntax_list(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    try:
        techs = list_technologies()
    except FileNotFoundError as e:
        return {"ok": False, "error_code": "data_dir_missing", "message": str(e)}
    total = sum(t["count"] for t in techs)
    return {
        "ok": True,
        "skill_id": "syntax.list",
        "technologies": techs,
        "technology_count": len(techs),
        "total_snippets": total,
    }


async def syntax_get(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    technology = (args.get("technology") or args.get("language") or "").strip().lower()
    sid = (args.get("id") or "").strip()
    if not technology:
        return {"ok": False, "error_code": "invalid_args", "message": "technology required"}
    if not sid:
        return {"ok": False, "error_code": "invalid_args", "message": "id required"}
    try:
        loaded = _load_technology(technology)
    except FileNotFoundError as e:
        return {"ok": False, "error_code": str(e), "message": str(e)}
    rec = loaded["by_id"].get(sid)
    if rec is None:
        return {
            "ok": False,
            "error_code": "not_found",
            "message": f"no syntax id={sid!r} in technology={technology!r}",
        }
    return {"ok": True, "skill_id": "syntax.get", "syntax": rec}


async def syntax_browse(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    technology = (args.get("technology") or args.get("language") or "").strip().lower()
    if not technology:
        return {"ok": False, "error_code": "invalid_args", "message": "technology required"}
    limit = max(1, min(int(args.get("limit") or 20), 200))
    offset = max(0, int(args.get("offset") or 0))
    construct = (args.get("construct") or "").strip().lower()
    try:
        loaded = _load_technology(technology)
    except FileNotFoundError as e:
        return {"ok": False, "error_code": str(e), "message": str(e)}
    rows = loaded["ordered"]
    if construct:
        rows = [r for r in rows if construct in r["construct"].lower()]
    page = rows[offset : offset + limit]
    return {
        "ok": True,
        "skill_id": "syntax.browse",
        "technology": technology,
        "count": len(page),
        "total_after_filter": len(rows),
        "total_in_technology": loaded["total"],
        "offset": offset,
        "limit": limit,
        "snippets": page,
    }


# Convenience (test helper).
def reset_cache() -> None:
    _CACHE.clear()
