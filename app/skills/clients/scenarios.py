"""scenarios.py — v1-compatible public surface over the operator's v2 matcher.

This shim exists so the 20 existing skill contracts
(github/openclaw/composio/firecrawl_steel/render.scenario.match
+ scenario.match unified + scenario.index RAG indexer) keep
working while the actual matching is done by the operator's
authoritative v2 algorithm in scenario_match_algo.py +
scenario_store.py.

Public functions (v1 shape preserved):
  - match_scenarios(trigger, pack="all", condition=None,
    category=None, context=None, limit=5, min_score=1.25)
    -> {query, count, matches, chosen, deferred, reason,
       scenarios_dir, packs_queried, score_threshold, scored_total}

Each match is a v1-shaped dict with id/pack/category/trigger/
condition/if_action/else_action/severity/source_doc/score and
any pack-specific extras (skill, service).
"""
from __future__ import annotations

from typing import Any

from .scenario_match_algo import match_scenarios as _v2_match_scenarios


def _to_v1_shape(v2: dict[str, Any], *, query: dict[str, Any] | None = None) -> dict[str, Any]:
    _v1_query = query or {}
    """Translate the operator's v2 result into the v1 result shape
    the existing 20 contracts + DEFER gate + tests expect."""
    matches_v2 = v2.get("matches") or []
    matches_v1: list[dict[str, Any]] = []
    for m in matches_v2:
        # Operator v2 row has: id, pack, category, skill, service,
        # trigger, condition, if_action, else_action, severity,
        # source_doc, score. Same columns as v1 plus skill/service
        # already preserved.
        out = {
            "id": m.get("id"),
            "pack": m.get("pack"),
            "category": m.get("category") or "",
            "trigger": m.get("trigger") or "",
            "condition": m.get("condition") or "",
            "if_action": m.get("if_action") or "",
            "else_action": m.get("else_action") or "",
            "severity": m.get("severity") or "",
            "source_doc": m.get("source_doc") or "",
            "score": m.get("score", 0.0),
        }
        for extra in ("skill", "service", "layer"):
            v = m.get(extra)
            if v:
                out[extra] = v
        matches_v1.append(out)
    chosen = matches_v1[0] if matches_v1 else None
    stats = v2.get("stats") or {}
    # echo back the trigger that was actually matched (the v2 algo
    # doesn't return it directly; we get it from the call site via
    # the function arg).
    return {
        "query": {"trigger": _v1_query.get("trigger", ""),
                  "condition": _v1_query.get("condition"),
                  "category": _v1_query.get("category"),
                  "pack": stats.get("pack", "all")},
        "count": len(matches_v1),
        "matches": matches_v1,
        "chosen": chosen,
        "deferred": bool(v2.get("deferred", False)),
        "reason": v2.get("reason", ""),
        "scenarios_dir": stats.get("store", {}).get("data_dir", ""),
        "packs_queried": ([stats["pack"]] if stats.get("pack") and stats["pack"] != "all"
                           else sorted((stats.get("store") or {}).get("packs") or [])),
        "score_threshold": stats.get("min_score", 1.25),
        "scored_total": stats.get("above_min", 0),
        # Operator v2 extras — useful for callers that want them
        "query_tokens": v2.get("query_tokens", []),
        "stats": stats,
    }


_VALID_PACKS = frozenset({
    "all", "github", "openclaw", "composio", "firecrawl_steel", "render",
    "aion_stack",
})


_VALID_LAYERS = frozenset({
    "scenarios", "books_rag", "code_corpus", "tools", "kernel",
})


def match_scenarios(
    trigger: str,
    *,
    pack: str = "all",
    condition: str | None = None,
    category: str | None = None,
    layer: str | None = None,
    context: dict[str, Any] | None = None,
    limit: int = 5,
    min_score: float = 1.25,
) -> dict[str, Any]:
    """Operator v2 matcher behind the v1 public surface. Returns the
    v1 result shape so the 20 existing skill contracts and the
    DEFER gate in main.py keep working unchanged. The `layer` param
    filters rows to a single logical layer of the aion_stack pack
    (scenarios / books_rag / code_corpus / tools / kernel)."""
    from ..base import SkillError
    import os as _os
    if pack and pack not in _VALID_PACKS:
        raise SkillError(
            "invalid_args",
            f"unknown_pack:{pack}",
        )
    # Forensic P1#6: pack="all" honors SCENARIO_DEFAULT_PACKS env
    # (default = 5 production packs, openclaw excluded). The filter
    # is applied AFTER the v2 matcher returns so the matcher keeps
    # using its native pack="all" expansion; we just drop the
    # excluded packs from the matches. Operators can opt back into
    # openclaw by setting
    # SCENARIO_DEFAULT_PACKS=github,openclaw,composio,firecrawl_steel,render,aion_stack
    _default_excluded: set[str] = set()
    _env_default_packs = _os.environ.get("SCENARIO_DEFAULT_PACKS", "").strip()
    if _env_default_packs:
        _default_excluded = (
            _VALID_PACKS
            - {p.strip() for p in _env_default_packs.split(",") if p.strip()}
            - {"all"}
        )
    else:
        # Default excludes openclaw from the unified view
        _default_excluded = {"openclaw"}
    _is_all = (pack == "all")
    if layer and layer not in _VALID_LAYERS:
        raise SkillError(
            "invalid_args",
            f"unknown_layer:{layer}",
        )
    # If pack="all", ask v2 to over-fetch so the post-filter in
    # _to_v1_shape has enough headroom.
    _fetch_limit = max(limit * 4, 20) if _is_all else limit
    v2 = _v2_match_scenarios(
        trigger=trigger,
        pack=pack,
        category=category,
        layer=layer,
        context=context,
        limit=_fetch_limit,
        min_score=min_score,
    )
    out = _to_v1_shape(
        v2,
        query={
            "trigger": trigger,
            "condition": condition,
            "category": category,
            "layer": layer,
        },
    )
    if _is_all and _default_excluded:
        # Forensic P1#6 quarantine: drop excluded packs from the
        # returned matches. limit still applies after the filter.
        out["matches"] = [m for m in out["matches"] if m.get("pack") not in _default_excluded]
        out["count"] = len(out["matches"])
        if out["chosen"] and out["chosen"].get("pack") in _default_excluded:
            out["chosen"] = out["matches"][0] if out["matches"] else None
        out["quarantined_packs"] = sorted(_default_excluded)
    return out


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
            layer=args.get("layer"),
            context=args.get("context"),
            limit=int(args.get("limit") or 5),
            min_score=float(args.get("min_score") or 1.25),
        )
    _skill.__name__ = f"scenario_match_{pack}"
    return _skill


github_scenario_match          = _make_skill("github")
openclaw_scenario_match        = _make_skill("openclaw")
composio_scenario_match        = _make_skill("composio")
firecrawl_steel_scenario_match = _make_skill("firecrawl_steel")
render_scenario_match          = _make_skill("render")
aion_stack_scenario_match      = _make_skill("aion_stack")


async def scenario_match_all(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Unified skill: search all packs (incl. aion_stack) at once.
    Optional `layer` filter narrows the aion_stack rows to one of
    scenarios / books_rag / code_corpus / tools / kernel."""
    return match_scenarios(
        trigger=args.get("trigger", ""),
        pack="all",
        condition=args.get("condition"),
        category=args.get("category"),
        layer=args.get("layer"),
        context=args.get("context"),
        limit=int(args.get("limit") or 10),
        min_score=float(args.get("min_score") or 1.25),
    )


async def scenario_index(
    args: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Index policy rows into the local RAG so rag.skills.search can find
    them by natural language."""
    from .scenario_store import get_store
    from ..rag.store import get_rag_store
    pack = (args.get("pack") or "all").strip().lower()
    store = get_store()
    store.ensure_loaded()
    rag = get_rag_store()
    if pack == "all":
        rows = list(store.iter_rows(pack=None))
        collection = "scenario_policy"
    elif pack in store.packs:
        rows = list(store.iter_rows(pack=pack))
        collection = f"scenario_policy_{pack}"
    else:
        from ..base import SkillError
        raise SkillError("invalid_args", f"unknown_pack:{pack}")
    n = 0
    for r in rows:
        text = (
            f"{r.id} | pack={r.pack}\n"
            f"category: {r.category}\n"
            f"trigger: {r.trigger}\n"
            f"condition: {r.condition}\n"
            f"if_action: {r.if_action}\n"
            f"else_action: {r.else_action}\n"
            f"severity: {r.severity}\n"
        )
        rag.upsert(
            collection, text,
            source=r.id,
            meta={"id": r.id, "pack": r.pack, "category": r.category},
        )
        n += 1
    return {
        "indexed": n,
        "collection": collection,
        "packs_indexed": ([pack] if pack != "all" else list(store.packs)),
        "scenarios_dir": str(store.data_dir),
    }
