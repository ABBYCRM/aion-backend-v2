"""GitHub scenario matcher — lookup policy rows by trigger + condition.

The CSV is the source of truth. The model never invents a row.
The skill returns ranked matches from the CSV; the operator sees
the if_action / else_action / severity and decides what to run.

The matcher is read-only (side_effect: "read"). It cannot mutate
the policy; an LLM cannot smuggle a fake row through this path.
"""
from __future__ import annotations

import csv
import os
import re
from pathlib import Path
from typing import Any

from ..base import SkillError


DEFAULT_CSV_PATH = os.environ.get("AION_GITHUB_SCENARIOS_CSV") or (
    Path(os.environ.get("AION_DATA_DIR") or "./data") / "github_scenarios.csv"
)


def resolve_csv_path() -> Path:
    """Pick the first CSV path that exists, in priority order:
    1. $AION_GITHUB_SCENARIOS_CSV (operator override)
    2. $AION_DATA_DIR/github_scenarios.csv (DO volume)
    3. /app/data/github_scenarios.csv (Docker image)
    4. ./data/github_scenarios.csv (local dev)
    Returns the first path that exists, falling back to the highest-priority
    candidate so the caller can produce a clear load_failed error.
    """
    candidates: list[Path] = []
    env_override = os.environ.get("AION_GITHUB_SCENARIOS_CSV")
    if env_override:
        candidates.append(Path(env_override))
    data_dir = os.environ.get("AION_DATA_DIR")
    if data_dir:
        candidates.append(Path(data_dir) / "github_scenarios.csv")
    candidates.append(Path("/app/data/github_scenarios.csv"))
    candidates.append(Path("./data/github_scenarios.csv"))
    for c in candidates:
        if c.exists():
            return c
    return candidates[0] if candidates else Path("./data/github_scenarios.csv")


def _normalize(value: str) -> str:
    return re.sub(r"\W+", " ", (value or "").lower()).strip()


def _tokens(value: str) -> set[str]:
    return {t for t in _normalize(value).split() if len(t) > 2}


def _load_rows(csv_path: str | Path) -> list[dict[str, str]]:
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise SkillError("github_scenarios_load_failed", f"csv_not_found:{csv_path}")
    rows: list[dict[str, str]] = []
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Strip any unexpected columns; keep the contract 8
            rows.append({k: (v or "") for k, v in row.items() if k in {
                "id", "category", "trigger", "condition",
                "if_action", "else_action", "severity", "source_doc",
            }})
    return rows


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


def match_scenarios(
    trigger: str,
    *,
    condition: str | None = None,
    category: str | None = None,
    context: dict[str, Any] | None = None,
    csv_path: str | Path | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Match the trigger (+ optional condition / category / context) against
    the loaded CSV. Returns ranked matches. Never invents a row."""
    trig = (trigger or "").strip()
    if not trig:
        raise SkillError("invalid_args", "missing_required:trigger")
    cond = (condition or "").strip()
    cat = (category or "").strip().lower() or None
    effective_csv = Path(csv_path) if csv_path else resolve_csv_path()
    rows = _load_rows(effective_csv)
    if not rows:
        raise SkillError("github_scenarios_empty", f"csv_empty:{csv_path}")
    # Filter
    if cat:
        rows = [r for r in rows if r.get("category", "").lower() == cat]
        if not rows:
            raise SkillError("github_scenarios_empty", f"no_rows_in_category:{cat}")
    query_tokens = _tokens(trig) | (_tokens(cond) if cond else set())
    if not query_tokens:
        raise SkillError("invalid_args", "trigger_and_condition_too_short")
    # Score + rank
    scored: list[tuple[float, dict[str, str]]] = []
    for row in rows:
        s = _score(query_tokens, row)
        if s > 0:
            scored.append((s, row))
    scored.sort(key=lambda x: -x[0])
    top = [
        {
            "id": row["id"],
            "category": row.get("category", ""),
            "trigger": row.get("trigger", ""),
            "condition": row.get("condition", ""),
            "if_action": row.get("if_action", ""),
            "else_action": row.get("else_action", ""),
            "severity": row.get("severity", ""),
            "source_doc": row.get("source_doc", ""),
            "score": round(s, 2),
        }
        for s, row in scored[: max(1, min(limit, 50))]
    ]
    chosen = top[0] if top else None
    reason = (
        f"matched {len(scored)} row(s) on token overlap; "
        f"returned top {len(top)} (best score {chosen['score']})"
        if chosen else
        f"matched 0 rows for trigger='{trig}'"
        f"{' category=' + category if category else ''}"
    )
    return {
        "query": {"trigger": trig, "condition": cond or None, "category": cat},
        "count": len(top),
        "matches": top,
        "chosen": chosen,
        "reason": reason,
        "csv_path": str(effective_csv),
        "csv_rows_total": len(rows),
    }


# The skill entry point signature matches the runner contract:
# async (args, ctx) -> dict
async def github_scenario_match(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    explicit = args.get("csv_path")
    return match_scenarios(
        trigger=args.get("trigger", ""),
        condition=args.get("condition"),
        category=args.get("category"),
        context=args.get("context"),
        csv_path=explicit or None,
        limit=int(args.get("limit") or 5),
    )


async def github_scenario_index(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Subroutine: index the CSV into the local RAG 'github_policy' collection
    so rag.skills.search can find scenarios by natural language too."""
    from ..rag.store import get_rag_store
    explicit = args.get("csv_path")
    effective_csv = Path(explicit) if explicit else resolve_csv_path()
    rows = _load_rows(effective_csv)
    if not rows:
        raise SkillError("github_scenarios_empty", f"csv_empty:{args.get('csv_path') or DEFAULT_CSV_PATH}")
    store = get_rag_store()
    n = 0
    for r in rows:
        text = (
            f"{r['id']}\n"
            f"category: {r.get('category', '')}\n"
            f"trigger: {r.get('trigger', '')}\n"
            f"condition: {r.get('condition', '')}\n"
            f"if_action: {r.get('if_action', '')}\n"
            f"else_action: {r.get('else_action', '')}\n"
            f"severity: {r.get('severity', '')}"
        )
        store.upsert("github_policy", text, source=r.get("id", ""), meta={"id": r.get("id", ""), "category": r.get("category", "")})
        n += 1
    return {"indexed": n, "collection": "github_policy", "csv_rows_total": len(rows), "csv_path": str(effective_csv)}
