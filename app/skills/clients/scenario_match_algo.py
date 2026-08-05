"""
Scenario matching algorithms for AION.

Pipeline:
  1. Hard filters (pack, category, skill, service, severity_min)
  2. Tokenize query (stopwords stripped)
  3. Weighted score:
       - trigger overlap
       - condition overlap (high weight — many policies put signal in condition)
       - union blob term
       - status-code boost (401/429/…)
       - structured context
       - category/skill/service boost
       - severity boost on error-looking queries
  4. Drop score < min_score (default 1.25)
  5. Sort score desc → top limit
  6. Empty → deferred=True (caller DEFERs; no invented rows)

No LLM. Deterministic for same store + inputs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .scenario_store import ScenarioRow, ScenarioStore, get_store, tokenize

_ERROR_HINTS = frozenset(
    {
        "error", "fail", "failed", "failure", "denied", "forbidden", "unauthorized",
        "timeout", "timed", "rate", "limit", "429", "401", "403", "404", "500",
        "502", "503", "invalid", "missing", "expired", "blocked", "reject",
        "rejected", "crash", "oom", "exception", "secondary",
    }
)

_STATUS_RE = re.compile(r"\b([1-5][0-9]{2})\b")
_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass
class MatchConfig:
    limit: int = 5
    min_score: float = 1.25
    trigger_weight: float = 1.0
    condition_weight: float = 0.95
    blob_weight: float = 0.35
    category_boost: float = 0.75
    skill_boost: float = 0.75
    service_boost: float = 0.75
    severity_error_boost: float = 0.25
    context_boost: float = 1.0
    status_code_boost: float = 1.5


def _context_tokens(context: Optional[Dict[str, Any]]) -> frozenset:
    if not context:
        return frozenset()
    parts: List[str] = []
    for k, v in context.items():
        if v is None:
            continue
        parts.append(str(k))
        parts.append(str(v))
        if isinstance(v, dict):
            for kk, vv in v.items():
                parts.append(str(kk))
                parts.append(str(vv))
    return tokenize(" ".join(parts))


def score_row(
    query_tokens: frozenset,
    row: ScenarioRow,
    *,
    category: Optional[str] = None,
    skill: Optional[str] = None,
    service: Optional[str] = None,
    context_tokens: frozenset = frozenset(),
    query_has_error_hint: bool = False,
    cfg: MatchConfig,
) -> float:
    if not query_tokens:
        return 0.0

    blob = row.trigger_tokens | row.condition_tokens

    trig_hit = len(query_tokens & row.trigger_tokens)
    score = cfg.trigger_weight * float(trig_hit)

    if row.condition_tokens:
        cond_hit = len(query_tokens & row.condition_tokens)
        score += cfg.condition_weight * float(cond_hit)

    blob_hit = len(query_tokens & blob)
    score += cfg.blob_weight * float(blob_hit)

    if context_tokens:
        ctx_hit = len(context_tokens & blob)
        score += cfg.context_boost * float(ctx_hit)
        for t in context_tokens:
            if t.isdigit() and len(t) == 3 and t in blob:
                score += cfg.status_code_boost

    for t in query_tokens:
        if t.isdigit() and len(t) == 3 and t in blob:
            score += cfg.status_code_boost * 0.5

    if category and row.category == category:
        score += cfg.category_boost
    if skill and row.skill and row.skill == skill:
        score += cfg.skill_boost
    if service and row.service and row.service == service:
        score += cfg.service_boost

    if query_has_error_hint and _SEVERITY_RANK.get(row.severity, 1) >= 2:
        score += cfg.severity_error_boost

    return score


def match_scenarios(
    trigger: str,
    *,
    pack: Optional[str] = "all",
    category: Optional[str] = None,
    skill: Optional[str] = None,
    service: Optional[str] = None,
    severity_min: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    limit: int = 5,
    min_score: float = 1.25,
    store: Optional[ScenarioStore] = None,
    config: Optional[MatchConfig] = None,
) -> Dict[str, Any]:
    """
    Rank scenario rows for a free-text trigger.

    Returns matches, reason, deferred, query_tokens, stats.
    deferred=True when nothing clears min_score — caller must DEFER.
    """
    cfg = config or MatchConfig(limit=limit, min_score=min_score)
    cfg.limit = max(1, min(int(limit or cfg.limit), 20))
    cfg.min_score = float(min_score if min_score is not None else cfg.min_score)

    trigger = (trigger or "").strip()
    if not trigger:
        return {
            "matches": [],
            "reason": "empty trigger — DEFER",
            "query_tokens": [],
            "deferred": True,
            "stats": {"candidates": 0},
        }

    st = store or get_store()
    st.ensure_loaded()

    q_tokens = tokenize(trigger)
    ctx = _context_tokens(context)
    for m in _STATUS_RE.finditer(trigger):
        ctx = ctx | frozenset({m.group(1)})

    query_has_error = bool(q_tokens & _ERROR_HINTS) or bool(ctx & _ERROR_HINTS)

    candidates: List[ScenarioRow] = list(
        st.iter_rows(
            pack=pack,
            category=category,
            skill=skill,
            service=service,
            severity_min=severity_min,
        )
    )

    scored: List[tuple[float, ScenarioRow]] = []
    for row in candidates:
        s = score_row(
            q_tokens,
            row,
            category=category,
            skill=skill,
            service=service,
            context_tokens=ctx,
            query_has_error_hint=query_has_error,
            cfg=cfg,
        )
        if s >= cfg.min_score:
            scored.append((s, row))

    scored.sort(key=lambda x: (-x[0], x[1].id))
    top = scored[: cfg.limit]

    matches = [row.to_dict(score=s) for s, row in top]
    deferred = len(matches) == 0

    if deferred:
        reason = (
            f"no scenario above min_score={cfg.min_score} "
            f"(candidates={len(candidates)}, pack={pack or 'all'}) — DEFER"
        )
    else:
        reason = (
            f"token+weighted match pack={pack or 'all'} "
            f"returned={len(matches)} candidates={len(candidates)}"
        )

    return {
        "matches": matches,
        "reason": reason,
        "query_tokens": sorted(q_tokens),
        "deferred": deferred,
        "stats": {
            "candidates": len(candidates),
            "above_min": len(scored),
            "pack": pack or "all",
            "category": category,
            "skill": skill,
            "service": service,
            "min_score": cfg.min_score,
            "limit": cfg.limit,
            "store": st.stats(),
        },
    }


def match(
    trigger: str,
    *,
    pack: str = "all",
    category: Optional[str] = None,
    skill: Optional[str] = None,
    service: Optional[str] = None,
    severity_min: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    limit: int = 5,
    min_score: float = 1.25,
    store: Optional[ScenarioStore] = None,
    config: Optional[MatchConfig] = None,
) -> Dict[str, Any]:
    """Alias for match_scenarios; v1-shape adds  and ."""
    out = match_scenarios(
        trigger,
        pack=pack,
        category=category,
        skill=skill,
        service=service,
        severity_min=severity_min,
        context=context,
        limit=limit,
        min_score=min_score,
        store=store,
        config=config,
    )
    matches = out.get("matches") or []
    out["count"] = len(matches)
    out["chosen"] = matches[0] if matches else None
    return out


def match_by_id(scenario_id: str, store: Optional[ScenarioStore] = None) -> Optional[Dict[str, Any]]:
    st = store or get_store()
    st.ensure_loaded()
    sid = (scenario_id or "").strip()
    for r in st.iter_rows(pack="all"):
        if r.id == sid:
            return r.to_dict()
    return None
