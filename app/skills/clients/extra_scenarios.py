"""
AION extra scenarios client — 2,900,000 cross-language coding scenarios.

Operator pack: 100k_scenarios_additional_29 (29 .txt files, one per
language/technology, 100,000 scenarios each, ~14MB per file, ~400MB total).

Format (per line, after 2 comment lines):
    id | domain | concept | action | constraint | failure
Example:
    000001 | identity | pipelines | design the component | low memory | handle timeouts; prove with tests and runtime evidence.

Design choices:
  * Lazy load per language — only the languages queried live in memory.
  * Per-language in-memory layout: dict[id, scenario] for O(1) get;
    list[scenario] for search/random/browse.
  * Search runs on a SINGLE language (100k rows), not across all
    2.9M, to keep latency low and results stable.
  * 5 skill entry points: list / get / search / random / browse.

Skills (registered in seed_all.py, 5 contracts):
  - extra.scenarios.list    : return all 29 languages + counts
  - extra.scenarios.get     : fetch one scenario by id (language + id)
  - extra.scenarios.search  : token-overlap search in one language
  - extra.scenarios.random  : N random scenarios from one language (drills)
  - extra.scenarios.browse  : paginate one language; optional concept filter
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("aion.skills.extra_scenarios")

DATA_DIR_NAME = "extra_scenarios"
# Stable id -> technology display name (matches the operator manifest).
# We reverse-engineer the technology from the file stem: "rust" -> "Rust".
TECHNOLOGY_DISPLAY: dict[str, str] = {
    "assembly": "Assembly", "bash": "Bash", "c_sharp": "C Sharp", "clojure": "Clojure",
    "cobol": "COBOL", "dart": "Dart", "elixir": "Elixir", "erlang": "Erlang",
    "f_sharp": "F Sharp", "fortran": "Fortran", "go": "Go", "haskell": "Haskell",
    "julia": "Julia", "kotlin": "Kotlin", "lua": "Lua", "matlab": "MATLAB",
    "objective-c": "Objective-C", "ocaml": "OCaml", "perl": "Perl", "php": "PHP",
    "powershell": "PowerShell", "r": "R", "ruby": "Ruby", "rust": "Rust",
    "scala": "Scala", "solidity": "Solidity", "sql": "SQL", "swift": "Swift",
    "zig": "Zig",
}

# Process-wide cache: language -> {by_id, ordered_list, total}
_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK_NAME = "_EXTRA_SCENARIOS_LOCK"

# Stopwords for the search token-overlap scorer.
_STOPWORDS = frozenset(
    "a an the and or to of in on for is are be with as by at from that this it its into via per not no yes if then else when while agent operator user".split()
)

# Stable token regex.
_TOKEN_RE = re.compile(r"[a-z0-9_+#-]+")


def _data_roots() -> list[Path]:
    """Search paths for the extra_scenarios directory.

    Order: $AION_EXTRA_SCENARIOS_DIR, $AION_DATA_DIR, package-relative,
    cwd, /app/data, /app/data/extra_scenarios.
    """
    roots: list[Path] = []
    for key in ("AION_EXTRA_SCENARIOS_DIR", "AION_DATA_DIR"):
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
        if root.is_dir() and any(root.glob("*_100k_scenarios.txt")):
            return root
    raise FileNotFoundError(
        "extra_scenarios data dir not found; copy the 29 *_100k_scenarios.txt "
        "files into data/extra_scenarios/ or set $AION_EXTRA_SCENARIOS_DIR."
    )


def list_languages() -> list[dict[str, Any]]:
    """Return the catalog of 29 languages. Cheap (no file load needed)."""
    data_dir = resolve_data_dir()
    out: list[dict[str, Any]] = []
    for slug, display in TECHNOLOGY_DISPLAY.items():
        path = data_dir / f"{slug}_100k_scenarios.txt"
        if path.is_file():
            # Count non-comment lines (each = 1 scenario).
            n = 0
            with path.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    n += 1
            out.append(
                {
                    "language": slug,
                    "technology": display,
                    "file": path.name,
                    "count": n,
                    "size_bytes": path.stat().st_size,
                }
            )
    out.sort(key=lambda r: r["language"])
    return out


def _load_language(language: str) -> dict[str, Any]:
    """Lazy-load one language file into a {by_id, ordered} dict."""
    language = (language or "").strip().lower()
    if language not in TECHNOLOGY_DISPLAY:
        raise FileNotFoundError(f"unknown_language:{language}")
    if language in _CACHE:
        return _CACHE[language]
    data_dir = resolve_data_dir()
    path = data_dir / f"{language}_100k_scenarios.txt"
    if not path.is_file():
        raise FileNotFoundError(f"missing file: {path}")
    by_id: dict[str, dict[str, str]] = {}
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 6:
                continue
            sid, domain, concept, action, constraint, failure = parts[:6]
            rec = {
                "id": sid,
                "language": language,
                "technology": TECHNOLOGY_DISPLAY[language],
                "domain": domain,
                "concept": concept,
                "action": action,
                "constraint": constraint,
                "failure": failure,
            }
            by_id[sid] = rec
    # Pre-compute tokenized blob per record so search is O(N) on
    # token set intersections only, not re-tokenization.
    for rec in by_id.values():
        rec["_blob_tokens"] = _tokens(
            " ".join([rec["domain"], rec["concept"], rec["action"],
                      rec["constraint"], rec["failure"]])
        )
    _CACHE[language] = {
        "by_id": by_id,
        "ordered": list(by_id.values()),  # stable iteration order
        "total": len(by_id),
        "path": str(path),
    }
    return _CACHE[language]


def _tokens(s: str) -> set[str]:
    return {
        t for t in _TOKEN_RE.findall((s or "").lower()) if len(t) > 2 and t not in _STOPWORDS
    }


# ===========================================================================
# Skill entry points
# ===========================================================================

async def extra_scenarios_list(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Return the catalog of 29 languages (no scenario load)."""
    try:
        languages = list_languages()
    except FileNotFoundError as e:
        return {"ok": False, "error_code": "data_dir_missing", "message": str(e)}
    total = sum(l["count"] for l in languages)
    return {
        "ok": True,
        "skill_id": "extra.scenarios.list",
        "languages": languages,
        "language_count": len(languages),
        "total_scenarios": total,
    }


async def extra_scenarios_get(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Fetch one scenario by language + id."""
    language = (args.get("language") or "").strip().lower()
    sid = (args.get("id") or "").strip()
    if not language:
        return {"ok": False, "error_code": "invalid_args", "message": "language required"}
    if not sid:
        return {"ok": False, "error_code": "invalid_args", "message": "id required"}
    try:
        loaded = _load_language(language)
    except FileNotFoundError as e:
        return {"ok": False, "error_code": str(e), "message": str(e)}
    rec = loaded["by_id"].get(sid)
    if rec is None:
        return {
            "ok": False,
            "error_code": "not_found",
            "message": f"no scenario id={sid!r} in language={language!r}",
        }
    return {"ok": True, "skill_id": "extra.scenarios.get", "scenario": rec}


async def extra_scenarios_search(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Token-overlap search within ONE language (not across all 2.9M)."""
    language = (args.get("language") or "").strip().lower()
    query = (args.get("query") or "").strip()
    if not language:
        return {"ok": False, "error_code": "invalid_args", "message": "language required"}
    if not query:
        return {"ok": False, "error_code": "invalid_args", "message": "query required"}
    limit = max(1, min(int(args.get("limit") or 10), 100))
    min_score = float(args.get("min_score") if args.get("min_score") is not None else 1.0)
    try:
        loaded = _load_language(language)
    except FileNotFoundError as e:
        return {"ok": False, "error_code": str(e), "message": str(e)}
    q = _tokens(query)
    if not q:
        return {
            "ok": True, "skill_id": "extra.scenarios.search",
            "query": query, "language": language,
            "count": 0, "hits": [], "reason": "empty_query_tokens",
        }
    scored: list[tuple[float, dict[str, str]]] = []
    for rec in loaded["ordered"]:
        overlap = len(q & rec["_blob_tokens"])
        if overlap >= min_score:
            scored.append((float(overlap), rec))
    scored.sort(key=lambda x: (-x[0], x[1]["id"]))
    top = scored[:limit]
    return {
        "ok": True,
        "skill_id": "extra.scenarios.search",
        "language": language,
        "query": query,
        "count": len(top),
        "total_candidates": loaded["total"],
        "min_score": min_score,
        "hits": [
            {
                "id": r["id"], "language": r["language"],
                "domain": r["domain"], "concept": r["concept"],
                "action": r["action"], "constraint": r["constraint"],
                "failure": r["failure"], "score": s,
            }
            for s, r in top
        ],
    }


async def extra_scenarios_random(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Pick N random scenarios from a language (drills / sampling)."""
    language = (args.get("language") or "").strip().lower()
    if not language:
        return {"ok": False, "error_code": "invalid_args", "message": "language required"}
    n = max(1, min(int(args.get("n") or 5), 100))
    seed = args.get("seed")
    try:
        loaded = _load_language(language)
    except FileNotFoundError as e:
        return {"ok": False, "error_code": str(e), "message": str(e)}
    rng = random.Random(seed)
    picked = rng.sample(loaded["ordered"], k=min(n, loaded["total"]))
    return {
        "ok": True,
        "skill_id": "extra.scenarios.random",
        "language": language,
        "n": len(picked),
        "seed": seed,
        "scenarios": picked,
    }


async def extra_scenarios_browse(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Paginate one language; optional concept / domain / constraint filter."""
    language = (args.get("language") or "").strip().lower()
    if not language:
        return {"ok": False, "error_code": "invalid_args", "message": "language required"}
    limit = max(1, min(int(args.get("limit") or 20), 200))
    offset = max(0, int(args.get("offset") or 0))
    concept = (args.get("concept") or "").strip().lower()
    domain = (args.get("domain") or "").strip().lower()
    constraint = (args.get("constraint") or "").strip().lower()
    try:
        loaded = _load_language(language)
    except FileNotFoundError as e:
        return {"ok": False, "error_code": str(e), "message": str(e)}
    rows = loaded["ordered"]
    if concept or domain or constraint:
        rows = [
            r for r in rows
            if (not concept or concept in r["concept"].lower())
            and (not domain or domain in r["domain"].lower())
            and (not constraint or constraint in r["constraint"].lower())
        ]
    page = rows[offset : offset + limit]
    return {
        "ok": True,
        "skill_id": "extra.scenarios.browse",
        "language": language,
        "count": len(page),
        "total_after_filter": len(rows),
        "total_in_language": loaded["total"],
        "offset": offset,
        "limit": limit,
        "scenarios": page,
    }


# Convenience: drop the process cache (test helper).
def reset_cache() -> None:
    _CACHE.clear()
