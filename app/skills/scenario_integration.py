"""
Helpers to wire scenario.match into AION tool / chat paths.

Usage pattern (main.py or tool runner):

  from app.skills.scenario_integration import policy_for_tool_error, format_policy_evidence

  if tool_errors:
      policy = policy_for_tool_error(tool_name="github", error_text=err, pack="github")
      if policy["deferred"]:
          # hard DEFER — do not call LLM for analysis
          text = policy["defer_text"]
          ...
      else:
          evidence = format_policy_evidence(policy["matches"])
          # inject evidence into context; model may only use listed actions
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .scenario_match import run as scenario_match_run


def policy_for_tool_error(
    *,
    tool_name: str,
    error_text: str,
    pack: Optional[str] = None,
    category: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    subject: Optional[str] = None,
    limit: int = 5,
    min_score: float = 4.0,
) -> Dict[str, Any]:
    """
    Build policy for a failed tool call.

    pack defaults from tool_name heuristics if not provided.
    subject is the resource the tool was acting on (e.g. repository
    name, URL, search query). When present, it's included in the
    defer_text so the operator sees WHICH call failed.
    """
    pack = (pack or _infer_pack(tool_name, error_text)).lower()
    trigger = f"{tool_name} error: {error_text}".strip()
    result = scenario_match_run(
        {
            "trigger": trigger,
            "pack": pack,
            "category": category,
            "context": context or {"tool": tool_name, "error": error_text},
            "limit": limit,
            "min_score": min_score,
        }
    )
    deferred = bool(result.get("deferred"))
    matches = result.get("matches") or []
    defer_text = _defer_text(tool_name, error_text, matches, result.get("reason") or "", subject=subject)
    chosen = matches[0] if matches else None
    return {
        "ok": result.get("ok", True),
        "deferred": deferred,
        "pack": pack,
        "matches": matches,
        "chosen": chosen,
        "reason": result.get("reason"),
        "defer_text": defer_text,
        "defer_audit_code": "chat.deferred_tool_failure_v2",
        "trigger": trigger,
        "stats": result.get("stats"),
    }


def policy_for_event(
    summary: str = "",
    *,
    event_type: str = "event",
    pack: str = "all",
    context: Optional[Dict[str, Any]] = None,
    limit: int = 5,
    min_score: float = 2.0,
) -> Dict[str, Any]:
    """Positional first arg is the summary (most common case).
     keyword is for the structured-call form."""
    if not summary and not event_type:
        summary = ""
    trigger = f"{event_type}: {summary}".strip()
    result = scenario_match_run(
        {
            "trigger": trigger,
            "pack": pack,
            "context": context,
            "limit": limit,
            "min_score": min_score,
        }
    )
    matches = result.get("matches") or []
    return {
        "ok": result.get("ok", True),
        "deferred": bool(result.get("deferred")),
        "matches": matches,
        "chosen": matches[0] if matches else None,
        "reason": result.get("reason"),
        "defer_text": _defer_text(event_type, summary, matches, result.get("reason") or ""),
        "stats": result.get("stats"),
    }


def format_policy_evidence(matches: List[Dict[str, Any]], max_rows: int = 5) -> str:
    """
    Compact evidence block for LLM context.
    Instructs: only choose among listed actions.
    """
    if not matches:
        return (
            "POLICY: no matching scenario. You MUST DEFER. "
            "Do not invent analysis or actions."
        )
    lines = [
        "POLICY (authoritative — only use actions below; do not invent):",
    ]
    for m in matches[:max_rows]:
        lines.append(
            f"- [{m.get('id')}] sev={m.get('severity')} score={m.get('score')}\n"
            f"  if: {m.get('if_action')}\n"
            f"  else: {m.get('else_action')}\n"
            f"  when: {(m.get('trigger') or '')[:160]}"
        )
    lines.append("END POLICY")
    return "\n".join(lines)


def _infer_pack(tool_name: str, error_text: str) -> str:
    t = f"{tool_name} {error_text}".lower()
    if any(k in t for k in ("github", "gh ", "workflow", "dependabot", "pull_request")):
        return "github"
    if any(k in t for k in ("render", "deploy", "health check", "blueprint")):
        return "render"
    if any(k in t for k in ("firecrawl", "scrape", "steel", "crawl", "browser")):
        return "firecrawl_steel"
    if any(k in t for k in ("composio", "connected_account", "tool router")):
        return "composio"
    return "all"


def _defer_text(
    name: str,
    error_text: str,
    matches: List[Dict[str, Any]],
    reason: str,
    *,
    subject: Optional[str] = None,
) -> str:
    subject_note = f" on {subject}" if subject else ""
    if matches:
        top = matches[0]
        return (
            f"DEFER: tool/event '{name}' failed{subject_note}.\n"
            f"Error: {error_text[:300]}\n"
            f"Policy {top.get('id')} (sev={top.get('severity')}): "
            f"{top.get('else_action') or top.get('if_action')}\n"
            f"Source: {top.get('source_doc') or 'scenario pack'}"
        )
    return (
        f"DEFER: tool/event '{name}' failed{subject_note} and no policy scenario matched.\n"
        f"Error: {error_text[:300]}\n"
        f"Reason: {reason}\n"
        f"Do not invent an analysis. Fix credentials/allowlist/config or add a scenario row."
    )
