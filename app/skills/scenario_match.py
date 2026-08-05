"""
AION skill: scenario.match

Lookup agentic policy rows (trigger → if/else) from scenario CSVs.
Deterministic. Empty result → deferred=True (caller must DEFER, not invent).

Skill contract
--------------
id: scenario.match
input:
  trigger: str                 # required
  pack?: str                   # github|openclaw|composio|firecrawl_steel|render|all
  category?: str
  skill?: str
  service?: str
  severity_min?: str           # low|medium|high|critical
  context?: object             # optional structured hints (status_code, conclusion, ...)
  limit?: int                  # default 5, max 20
  min_score?: float            # default 1.25
output:
  matches: list[object]
  reason: str
  deferred: bool
  query_tokens: list[str]
  stats: object

Also exposed:
  scenario.match.reload   — reload CSVs from disk
  scenario.match.stats    — store stats
  scenario.match.get      — by id
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .clients.scenario_match_algo import match_by_id, match_scenarios
from .clients.scenario_store import get_store

logger = logging.getLogger("aion.skills.scenario_match")

SKILL_ID = "scenario.match"
SKILL_VERSION = "1.0.0"

# --- registry metadata (importable by seed_all / runner) ---
SKILL_CONTRACT = {
    "id": SKILL_ID,
    "name": "Scenario Policy Match",
    "version": SKILL_VERSION,
    "description": (
        "Match fixed agentic policy scenarios (trigger/condition → if_action/else_action). "
        "No generation. Empty matches means DEFER."
    ),
    "side_effect": False,
    "timeout_seconds": 5,
    "error_codes": [
        "scenario_empty_trigger",
        "scenario_store_unavailable",
        "scenario_not_found",
    ],
    "input_schema": {
        "type": "object",
        "required": ["trigger"],
        "properties": {
            "trigger": {"type": "string", "minLength": 1},
            "pack": {
                "type": "string",
                "enum": [
                    "all",
                    "github",
                    "openclaw",
                    "composio",
                    "firecrawl_steel",
                    "render",
                ],
            },
            "category": {"type": "string"},
            "skill": {"type": "string"},
            "service": {"type": "string"},
            "severity_min": {
                "type": "string",
                "enum": ["low", "medium", "high", "critical"],
            },
            "context": {"type": "object"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "min_score": {"type": "number", "minimum": 0},
        },
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "matches": {"type": "array"},
            "reason": {"type": "string"},
            "deferred": {"type": "boolean"},
            "query_tokens": {"type": "array"},
            "stats": {"type": "object"},
        },
    },
}


def run(params: Dict[str, Any] | None = None, **kwargs: Any) -> Dict[str, Any]:
    """
    Execute scenario.match.

    Accepts either a single params dict or keyword args.
    """
    p = dict(params or {})
    p.update({k: v for k, v in kwargs.items() if v is not None})

    trigger = p.get("trigger")
    if trigger is None or str(trigger).strip() == "":
        return {
            "ok": False,
            "error_code": "scenario_empty_trigger",
            "matches": [],
            "reason": "trigger is required",
            "deferred": True,
            "query_tokens": [],
            "stats": {},
        }

    pack = (p.get("pack") or "all").strip().lower()
    if pack not in (
        "all",
        "github",
        "openclaw",
        "composio",
        "firecrawl_steel",
        "render",
    ):
        pack = "all"

    try:
        result = match_scenarios(
            str(trigger),
            pack=pack,
            category=_opt_str(p.get("category")),
            skill=_opt_str(p.get("skill")),
            service=_opt_str(p.get("service")),
            severity_min=_opt_str(p.get("severity_min")),
            context=p.get("context") if isinstance(p.get("context"), dict) else None,
            limit=int(p.get("limit") or 5),
            min_score=float(p["min_score"]) if p.get("min_score") is not None else 1.25,
        )
        result["ok"] = True
        result["skill_id"] = SKILL_ID
        result["skill_version"] = SKILL_VERSION
        return result
    except Exception as e:
        logger.exception("scenario.match failed")
        return {
            "ok": False,
            "error_code": "scenario_store_unavailable",
            "matches": [],
            "reason": f"match failed: {e}",
            "deferred": True,
            "query_tokens": [],
            "stats": {},
        }


def run_reload(params: Dict[str, Any] | None = None, **kwargs: Any) -> Dict[str, Any]:
    store = get_store()
    stats = store.reload()
    return {"ok": True, "skill_id": "scenario.match.reload", "stats": stats}


def run_stats(params: Dict[str, Any] | None = None, **kwargs: Any) -> Dict[str, Any]:
    store = get_store()
    return {"ok": True, "skill_id": "scenario.match.stats", "stats": store.stats()}


def run_get(params: Dict[str, Any] | None = None, **kwargs: Any) -> Dict[str, Any]:
    p = dict(params or {})
    p.update(kwargs)
    sid = _opt_str(p.get("id") or p.get("scenario_id"))
    if not sid:
        return {
            "ok": False,
            "error_code": "scenario_empty_trigger",
            "match": None,
            "reason": "id is required",
        }
    row = match_by_id(sid)
    if not row:
        return {
            "ok": False,
            "error_code": "scenario_not_found",
            "match": None,
            "reason": f"no scenario id={sid}",
        }
    return {"ok": True, "skill_id": "scenario.match.get", "match": row}


def _opt_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


# Optional: auto-register helpers for seed_all style bootstraps
def register_executors(registry: Any) -> None:
    """
    If your skills runner exposes registry.register(id, fn), call this at boot.
    """
    registry.register(SKILL_ID, run)
    registry.register("scenario.match.reload", run_reload)
    registry.register("scenario.match.stats", run_stats)
    registry.register("scenario.match.get", run_get)


# CLI smoke test: python -m app.skills.scenario_match "rate limit 429 github"
if __name__ == "__main__":
    import json
    import sys

    trig = " ".join(sys.argv[1:]) or "github api 429 rate limit"
    out = run({"trigger": trig, "pack": "github", "limit": 3})
    print(json.dumps(out, indent=2))
