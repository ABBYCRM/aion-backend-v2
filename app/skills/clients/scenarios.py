"""Unified scenario matcher — operator-supplied policy pack library.

Loads CSVs from data/scenarios/ (or /app/data/scenarios/ on DO, or
$AION_DATA_DIR/scenarios/ on the volume). Each pack has the same 8-9
columns: id, category, trigger, condition, if_action, else_action,
severity, source_doc (openclaw adds 'skill', firecrawl_steel adds 'service').

The CSV is the source of truth. The model never invents a row.
The skill returns ranked matches from the CSV; the operator sees
the if_action / else_action / severity and decides what to run.

5 packs ship on the AION backend:
  - github            (500 rows, actions / secrets / API / webhooks / branch / Dependabot / Pages)
  - openclaw          (500 rows, shell / fs / Gmail / Notion / Slack / browser / skills)
  - composio          (500 rows, sessions / auth / connected accounts / tool execute / MCP)
  - firecrawl_steel   (500 rows, scrape / crawl / map / search / Steel sessions / pipeline)
  - render            (500 rows, build / boot / health / runtime / scaling / env / API / DB)

Total: 2,500 policy rows the LLM can match against, never invent.
"""
from __future__ import annotations

import csv
import os
import re
from pathlib import Path
from typing import Any, Iterable

from ..base import SkillError


# The 5 packs the operator ships. Add a pack = add an entry here AND a CSV
# in data/scenarios/<name>_scenarios.csv. The matcher is read-only.
PACKS: dict[str, str] = {
    "github":          "github_scenarios.csv",
    "openclaw":        "openclaw_scenarios.csv",
    "composio":        "composio_scenarios.csv",
    "firecrawl_steel": "firecrawl_steel_scenarios.csv",
    "render":          "render_scenarios.csv",
}

# Columns each pack ships; not all columns are in every pack.
# (extra columns are preserved on the returned match dict)
STANDARD_COLS: tuple[str, ...] = (
    "id", "category", "trigger", "condition",
    "if_action", "else_action", "severity", "source_doc",
)


def _resolve_scenarios_dir() -> Path:
    """Return the first scenarios dir that exists, in priority order:
    1. $AION_SCENARIOS_DIR (operator override)
    2. $AION_DATA_DIR/scenarios/ (DO volume)
    3. /app/data/scenarios/ (Docker image)
    4. /app/data/ (Docker image, root data dir, where github_scenarios.csv lives)
    5. ./data/scenarios/ (local dev)
    """
    candidates: list[Path] = []
    override = os.environ.get("AION_SCENARIOS_DIR")
    if override:
        candidates.append(Path(override))
    data_dir = os.environ.get("AION_DATA_DIR")
    if data_dir:
        candidates.append(Path(data_dir) / "scenarios")
    candidates.append(Path("/app/data/scenarios"))
    candidates.append(Path("/app/data"))  # backward compat: github CSV is here
    candidates.append(Path("./data/scenarios"))
    candidates.append(Path("./data"))  # local dev root data dir
    for c in candidates:
        if c.exists() and c.is_dir():
            return c
    return candidates[0] if candidates else Path("./data/scenarios")


def _normalize(value: str) -> str:
    return re.sub(r"\W+", " ", (value or "").lower()).strip()


def _tokens(value: str) -> set[str]:
    return {t for t in _normalize(value).split() if len(t) > 2}


def _score(query_tokens: set[str], row: dict[str, str]) -> float:
    """Token-overlap score. Trigger matches are weighted 2x because the
    trigger is the primary routing key; conditions refine the match."""
    if not query_tokens:
        return 0.0
    trig = _tokens(row.get("trigger", ""))
    cond = _tokens(row.get("condition", ""))
    trig_overlap = len(query_tokens & trig)
    cond_overlap = len(query_tokens & cond)
    if trig_overlap == 0 and cond_overlap == 0:
        return 0.0
    return (trig_overlap * 2.0) + cond_overlap


def _load_pack(pack: str, scenarios_dir: Path) -> list[dict[str, str]]:
    """Load one pack CSV; return rows."""
    if pack not in PACKS:
        raise SkillError("invalid_args", f"unknown_pack:{pack}")
    csv_path = scenarios_dir / PACKS[pack]
    if not csv_path.exists():
        # Backward compat: github_scenarios.csv may be in parent dir
        if pack == "github":
            alt = scenarios_dir.parent / "github_scenarios.csv"
            if alt.exists():
                csv_path = alt
            else:
                raise SkillError("scenarios_load_failed", f"csv_not_found:{csv_path}")
        else:
            raise SkillError("scenarios_load_failed", f"csv_not_found:{csv_path}")
    rows: list[dict[str, str]] = []
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            rows.append({k: (v or "") for k, v in row.items()})
    return rows


def match_scenarios(
    trigger: str,
    *,
    pack: str = "all",
    condition: str | None = None,
    category: str | None = None,
    context: dict[str, Any] | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Match the trigger against one or all packs. Returns ranked matches
    from the CSV. Never invents a row. Empty match list = caller must DEFER.
    """
    trig = (trigger or "").strip()
    if not trig:
        raise SkillError("invalid_args", "missing_required:trigger")
    cond = (condition or "").strip()
    cat = (category or "").strip().lower() or None
    pack_filter = (pack or "all").strip().lower()
    scenarios_dir = _resolve_scenarios_dir()

    if pack_filter == "all":
        packs_to_query: list[str] = list(PACKS.keys())
    elif pack_filter in PACKS:
        packs_to_query = [pack_filter]
    else:
        raise SkillError("invalid_args", f"unknown_pack:{pack}")

    query_tokens = _tokens(trig) | (_tokens(cond) if cond else set())
    if not query_tokens:
        raise SkillError("invalid_args", "trigger_and_condition_too_short")

    all_rows: list[tuple[str, dict[str, str]]] = []
    for p in packs_to_query:
        try:
            rows = _load_pack(p, scenarios_dir)
        except SkillError:
            if pack_filter != "all":
                raise
            continue
        for r in rows:
            all_rows.append((p, r))

    if not all_rows:
        raise SkillError("scenarios_empty", f"no_packs_loaded:{scenarios_dir}")

    scored: list[tuple[float, str, dict[str, str]]] = []
    for p, row in all_rows:
        if cat and row.get("category", "").lower() != cat:
            continue
        s = _score(query_tokens, row)
        if s > 0:
            scored.append((s, p, row))
    scored.sort(key=lambda x: -x[0])

    top: list[dict[str, Any]] = []
    for s, p, row in scored[: max(1, min(limit, 50))]:
        m = {k: row.get(k, "") for k in STANDARD_COLS}
        for k, v in row.items():
            if k not in STANDARD_COLS:
                m[k] = v
        m["pack"] = p
        m["score"] = round(s, 2)
        top.append(m)

    chosen = top[0] if top else None
    reason = (
        f"matched {len(scored)} row(s) across {len(packs_to_query)} pack(s); "
        f"returned top {len(top)} (best score {chosen['score']}, pack={chosen['pack']})"
        if chosen else
        f"matched 0 rows for trigger='{trig}' pack='{pack_filter}'"
        f"{' category=' + category if category else ''}"
    )
    return {
        "query": {"trigger": trig, "condition": cond or None, "category": cat, "pack": pack_filter},
        "count": len(top),
        "matches": top,
        "chosen": chosen,
        "reason": reason,
        "scenarios_dir": str(scenarios_dir),
        "packs_queried": list(packs_to_query),
    }


# ===========================================================================
# Skill entry points — one async function per pack so the registry can wire
# each contract to a stable, named executor.
# ===========================================================================

def _make_skill(pack: str):
    async def _skill(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        return match_scenarios(
            trigger=args.get("trigger", ""),
            pack=args.get("pack") or pack,
            condition=args.get("condition"),
            category=args.get("category"),
            context=args.get("context"),
            limit=int(args.get("limit") or 5),
        )
    _skill.__name__ = f"scenario_match_{pack}"
    return _skill


github_scenario_match          = _make_skill("github")
openclaw_scenario_match        = _make_skill("openclaw")
composio_scenario_match        = _make_skill("composio")
firecrawl_steel_scenario_match = _make_skill("firecrawl_steel")
render_scenario_match          = _make_skill("render")


async def scenario_match_all(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Unified skill: search all 5 packs at once."""
    return match_scenarios(
        trigger=args.get("trigger", ""),
        pack="all",
        condition=args.get("condition"),
        category=args.get("category"),
        context=args.get("context"),
        limit=int(args.get("limit") or 10),
    )


async def scenario_index(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Index policy rows into the local RAG so rag.skills.search can find
    them by natural language."""
    from ..rag.store import get_rag_store
    pack = (args.get("pack") or "all").strip().lower()
    if pack == "all":
        packs_to_index: list[str] = list(PACKS.keys())
        collection = "scenario_policy"
    elif pack in PACKS:
        packs_to_index = [pack]
        collection = f"scenario_policy_{pack}"
    else:
        raise SkillError("invalid_args", f"unknown_pack:{pack}")

    scenarios_dir = _resolve_scenarios_dir()
    store = get_rag_store()
    n = 0
    for p in packs_to_index:
        rows = _load_pack(p, scenarios_dir)
        for r in rows:
            extras = " ".join(
                f"{k}: {r[k]}" for k in r if k not in STANDARD_COLS
            )
            text = (
                f"{r['id']} | pack={p}\n"
                f"category: {r.get('category', '')}\n"
                f"trigger: {r.get('trigger', '')}\n"
                f"condition: {r.get('condition', '')}\n"
                f"if_action: {r.get('if_action', '')}\n"
                f"else_action: {r.get('else_action', '')}\n"
                f"severity: {r.get('severity', '')}\n"
                f"{extras}"
            )
            store.upsert(
                collection, text,
                source=r.get("id", ""),
                meta={"id": r.get("id", ""), "pack": p, "category": r.get("category", "")},
            )
            n += 1
    return {
        "indexed": n,
        "collection": collection,
        "packs_indexed": packs_to_index,
        "scenarios_dir": str(scenarios_dir),
    }
