"""Answer mirror — post-generation self-audit + repair loop.

Implements the "did I actually resolve the task?" gate. After the
answer model produces a response, the mirror asks a different
auditor model to score the answer on 5 axes (resolved, value_added,
grounded, honest, novel) and produces a structured audit JSON.
If the audit fails, the mirror runs a silent repair pass and
re-audits. If the repair also fails, the user-visible response is
shipped as-is with a self_check SSE event flagging it as weak.

The mirror is opt-in (REFLECTOR_ENABLED env var, default off).

Hard rules:
- Broken audit JSON → fail closed (treated as failed audit, ship
  the original response with a weak self_check badge).
- Token budget: never exceed 1.5x the original answer's input+output
  tokens across the audit + repair calls combined.
- Auditor model: a different provider than the answer when possible
  (cheap answer → frontier audit). If only the answer provider is
  available, the audit uses a strict scoring prompt.
- Repair prompt: explicitly forbids rephrasing the user's input.
  The user-provided analysis is background context, NOT the answer.
- Repair is observability-only. The user already streamed the
  original answer. The repair is logged to the audit table for
  operator review; the user does not see a different answer.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

# ----------------------------------------------------------------------------
# System prompts
# ----------------------------------------------------------------------------

CRITIC_SYSTEM = """You are an answer-completeness auditor. You will receive the user's ORIGINAL REQUEST and a CANDIDATE ANSWER from another AI. Your job is to score the candidate answer on 5 axes and return JSON only — no prose, no markdown fence.

Axes to score (1-5 each, integers):
1. resolved — Did the answer actually address every question and complete every task the user asked for? A response that summarizes the user's own analysis without adding anything is NOT resolved.
2. value_added — Did the answer bring something the user didn't already know or hadn't already said? A response that just rephrases what the user wrote scores 1. A response that adds new data, new framing, new analysis, or new perspective scores 4-5.
3. grounded — If sources were available (web search, github, corpus), did the answer use them accurately? A response that ignores the provided evidence scores 1.
4. honest — If the answer couldn't fully resolve the request, did it say so explicitly? A response that pretends to have done the work scores 1. A response that flags uncertainty scores 5.
5. novel — Did the answer add new insight, or just restate? Score 1 for restate, 5 for genuinely new analysis.

Also produce:
- requested_items: list of every explicit question or task in the user's request
- answered_items: list of items the candidate actually addressed with new content
- missing_items: list of items the candidate did NOT address
- weak_items: list of items the candidate addressed but only superficially (rephrased user, gave generic answer, etc.)

Return JSON only:
{
  "resolved": true|false,
  "value_added": 1-5,
  "grounded": 1-5,
  "honest": 1-5,
  "novel": 1-5,
  "requested_items": ["..."],
  "answered_items": ["..."],
  "missing_items": ["..."],
  "weak_items": ["..."]
}

resolved may be true ONLY when missing_items and weak_items are both empty. If you cannot verify the answer, default resolved=false. Do not grade on a curve."""

REVISION_SYSTEM = """You are repairing an incomplete AI answer. You will receive:
- the user's ORIGINAL REQUEST
- the AI's CURRENT ANSWER (which failed an audit)
- an AUDIT listing what was missed

Your job: rewrite the answer so it ACTUALLY resolves the user's request. This rewrite is for internal review only — the user has already seen the original.

Hard rules:
- Answer every explicit question with new content. Do not rephrase what the user already said.
- If the user provided their own analysis, treat it as background context. The user is asking for a NEW perspective, NOT a recap of their own points.
- Do not praise the user's analysis, observations, or framing. Go straight to the answer.
- Do not discuss the audit, the mirror, or the fact that you're revising.
- Do not introduce new requirements the user didn't ask for.
- If you genuinely cannot resolve the request, say so explicitly.
- Return ONLY the rewritten answer. No explanations, no markdown fence, no preamble.

Output the improved final answer directly."""

# ----------------------------------------------------------------------------
# Auditor routing — pick a different provider than the answer when possible.
# ----------------------------------------------------------------------------

AUDITOR_FOR_ANSWER: dict[str, str] = {
    "openai": "anthropic",
    "anthropic": "openai",
    "moonshot": "openai",
    "nvidia": "openai",
    "openrouter": "openai",
    "bitdeer": "openai",
    "cloudflare": "openai",
    "helicone": "openai",
}

# ----------------------------------------------------------------------------
# Strict parser — fail closed. Mirrors the kernel's hard DEFER gate.
# ----------------------------------------------------------------------------

_LIST_KEYS = ("requested_items", "answered_items", "missing_items", "weak_items")
_INT_KEYS = ("value_added", "grounded", "honest", "novel")


def parse_audit(raw: str) -> dict[str, Any]:
    """Parse the critic's raw output. Fails closed on any ambiguity.

    A successful parse returns the full audit dict. A failed parse
    returns a failed audit with resolved=False and the parse error
    recorded. The mirror treats a failed parse exactly like a failed
    audit: the repair pass runs. If the repair also fails to produce
    a passing audit, the user gets a weak self_check badge. We never
    let broken verification count as success."""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines.pop(0)
        if lines and lines[-1].startswith("```"):
            lines.pop()
        text = "\n".join(lines).strip()
    try:
        audit = json.loads(text)
    except json.JSONDecodeError as exc:
        return _failed_parse(str(exc))
    if not isinstance(audit, dict):
        return _failed_parse("audit_not_object")
    if not isinstance(audit.get("resolved"), bool):
        return _failed_parse("resolved_not_bool")
    for key in _INT_KEYS:
        val = audit.get(key)
        if not isinstance(val, int) or not (1 <= val <= 5):
            return _failed_parse(f"{key}_not_int_1_5")
    for key in _LIST_KEYS:
        val = audit.get(key)
        if not isinstance(val, list) or not all(isinstance(item, str) for item in val):
            return _failed_parse(f"{key}_not_list_of_str")
    # Defensive: never allow resolved=true while listing missing or weak items
    if audit["resolved"] and (audit["missing_items"] or audit["weak_items"]):
        audit["resolved"] = False
    return audit


def _failed_parse(error: str) -> dict[str, Any]:
    return {
        "resolved": False,
        "value_added": 1,
        "grounded": 1,
        "honest": 1,
        "novel": 1,
        "requested_items": [],
        "answered_items": [],
        "missing_items": [f"audit_parse_error: {error}"],
        "weak_items": [],
        "parse_error": error,
    }


# ----------------------------------------------------------------------------
# Token budget — never exceed 1.5x the original answer's tokens
# ----------------------------------------------------------------------------

MAX_TOKEN_MULTIPLIER = 1.5
DEFAULT_AUDIT_TOKENS = 800
DEFAULT_REVISION_TOKENS = 1024


def estimate_tokens(text: str) -> int:
    """Rough token estimate. ~4 chars per token for English."""
    return max(1, len(text) // 4)


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------

class MirrorResult:
    """The outcome of running the mirror on an answer."""
    __slots__ = (
        "resolved", "passed", "audits", "answer_provider", "answer_model",
        "auditor_provider", "auditor_model", "attempts", "tokens_added",
        "latency_ms", "user_knew_already", "repair_text", "skip_reason",
    )

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolved": self.resolved,
            "passed": self.passed,
            "answer_model": f"{self.answer_provider}/{self.answer_model}",
            "auditor_provider": self.auditor_provider,
            "auditor_model": self.auditor_model,
            "attempts": self.attempts,
            "tokens_added": self.tokens_added,
            "latency_ms": self.latency_ms,
            "user_knew_already": self.user_knew_already,
            "has_repair": bool(self.repair_text),
            "audit": self.audits[-1] if self.audits else None,
            "skip_reason": self.skip_reason,
        }


async def run_mirror(
    *,
    answer: str,
    user_request: str,
    answer_provider: str,
    answer_model: str,
    original_input_tokens: int,
    call_audit_fn,  # async (provider, model, messages, max_tokens, temperature) -> (text, tokens_used)
    call_revision_fn,  # async (provider, model, messages, max_tokens, temperature) -> (text, tokens_used)
    enabled: bool = True,
) -> MirrorResult:
    """Run the mirror on a final answer. Returns a MirrorResult with the
    audit chain + repair text (for operator review). Never raises —
    failures are recorded in the result.

    Flow:
    1. Run audit (attempt 1).
    2. If audit passes → return resolved=True, no badge needed.
    3. If audit fails AND budget allows → run revision + re-audit.
    4. If revision audit passes → return resolved=True, log the repair.
    5. If both audits fail → return resolved=False, weak badge.

    The user-visible response is NEVER replaced. The repair is for
    the operator audit log and for the next call's context."""
    started = time.monotonic()
    result = MirrorResult(
        resolved=False, passed=False, audits=[], answer_provider=answer_provider,
        answer_model=answer_model, attempts=1, tokens_added=0,
        latency_ms=0, user_knew_already=False, repair_text="",
        skip_reason="", auditor_provider="", auditor_model="",
    )
    if not enabled:
        result.skip_reason = "disabled"
        result.latency_ms = int((time.monotonic() - started) * 1000)
        return result
    if not answer or not user_request:
        result.skip_reason = "empty_input"
        result.latency_ms = int((time.monotonic() - started) * 1000)
        return result
    if len(answer) < 80:
        # Too short to be a hollow recap; don't audit short answers
        result.resolved = True
        result.passed = True
        result.skip_reason = "too_short"
        result.latency_ms = int((time.monotonic() - started) * 1000)
        return result
    # Token budget
    answer_tokens = estimate_tokens(answer)
    user_request_tokens = estimate_tokens(user_request)
    max_budget = int((original_input_tokens + answer_tokens) * MAX_TOKEN_MULTIPLIER)
    # Pick auditor
    auditor_provider = AUDITOR_FOR_ANSWER.get(answer_provider, answer_provider)
    auditor_model = _pick_auditor_model(auditor_provider)
    result.auditor_provider = auditor_provider
    result.auditor_model = auditor_model
    # ---- Attempt 1: audit the original answer ----
    # Wrap the audit call in try/except — a provider failure here must
    # NEVER break the user-visible response. The mirror completes with
    # resolved=False and a recorded parse_error, which the UI treats as
    # "weak" (the same as an audit that came back with low scores).
    try:
        audit_raw, audit_tokens = await call_audit_fn(
            provider=auditor_provider,
            model=auditor_model,
            messages=[
                {"role": "system", "content": CRITIC_SYSTEM},
                {"role": "user", "content": _audit_prompt(user_request, answer)},
            ],
            max_tokens=DEFAULT_AUDIT_TOKENS,
            temperature=0.1,
        )
        result.tokens_added += audit_tokens
        audit = parse_audit(audit_raw)
    except Exception as exc:
        audit = _failed_parse(f"audit_call_failed: {exc}")
    result.audits.append(audit)
    # Check for the "user knew already" failure mode
    if user_request_tokens > 500 and audit.get("value_added", 5) <= 2:
        result.user_knew_already = True
    if audit["resolved"]:
        result.resolved = True
        result.passed = True
        result.latency_ms = int((time.monotonic() - started) * 1000)
        return result
    # ---- Audit failed. Try one silent repair pass within budget. ----
    if result.tokens_added < max_budget:
        try:
            revision, revision_tokens = await call_revision_fn(
                provider=answer_provider,
                model=answer_model,
                messages=[
                    {"role": "system", "content": REVISION_SYSTEM},
                    {"role": "user", "content": _revision_prompt(user_request, answer, audit)},
                ],
                max_tokens=DEFAULT_REVISION_TOKENS,
                temperature=0.4,
            )
        except Exception as exc:
            revision = ""
            revision_tokens = 0
            result.audits.append({"resolved": False, "parse_error": f"revision_call_failed: {exc}"})
        result.tokens_added += revision_tokens
        if revision and len(revision) > 40:
            result.repair_text = revision[:8000]  # cap for the audit log
            result.attempts = 2  # we generated a repair attempt
            # Re-audit the revision (NOT the user-visible response — the
            # revision is only for the operator audit log).
            if result.tokens_added < max_budget:
                try:
                    re_audit_raw, re_audit_tokens = await call_audit_fn(
                        provider=auditor_provider,
                        model=auditor_model,
                        messages=[
                            {"role": "system", "content": CRITIC_SYSTEM},
                            {"role": "user", "content": _audit_prompt(user_request, revision)},
                        ],
                        max_tokens=DEFAULT_AUDIT_TOKENS,
                        temperature=0.1,
                    )
                    result.tokens_added += re_audit_tokens
                    re_audit = parse_audit(re_audit_raw)
                except Exception as exc:
                    re_audit = _failed_parse(f"re_audit_call_failed: {exc}")
                result.audits.append(re_audit)
                if re_audit["resolved"]:
                    # Repair succeeded. We don't replace the user-visible
                    # response (they already saw the original stream), but
                    # the audit log records that a stronger version exists.
                    result.resolved = True
                    result.passed = True
    # Final state
    result.latency_ms = int((time.monotonic() - started) * 1000)
    return result


def _audit_prompt(user_request: str, answer: str) -> str:
    return (
        f"<ORIGINAL_REQUEST>\n{json.dumps(user_request)}\n</ORIGINAL_REQUEST>\n\n"
        f"<CANDIDATE_ANSWER>\n{json.dumps(answer)}\n</CANDIDATE_ANSWER>\n\n"
        f"Audit the candidate answer against the original request. Return JSON only."
    )


def _revision_prompt(user_request: str, answer: str, audit: dict[str, Any]) -> str:
    return (
        f"<ORIGINAL_REQUEST>\n{json.dumps(user_request)}\n</ORIGINAL_REQUEST>\n\n"
        f"<CURRENT_ANSWER>\n{json.dumps(answer)}\n</CURRENT_ANSWER>\n\n"
        f"<WHAT_YOU_FAILED_TO_ANSWER>\n{json.dumps(audit)}\n</WHAT_YOU_FAILED_TO_ANSWER>\n\n"
        f"Rewrite the answer to actually resolve the original request. "
        f"Do not rephrase what the user already said — bring new analysis. "
        f"Return only the improved answer."
    )


def _pick_auditor_model(provider: str) -> str:
    """Return a small cheap model for the auditor provider. The audit
    prompt is short and structured — no need for a frontier model."""
    defaults = {
        "openai": "gpt-4o-mini",
        "anthropic": "claude-3-5-haiku-20241022",
        "moonshot": "moonshot-v1-8k",
        "nvidia": "meta/llama-3.1-8b-instruct",
        "openrouter": "openai/gpt-4o-mini",
        "bitdeer": "meta-llama/llama-3.1-8b-instruct",
        "cloudflare": "@cf/meta/llama-3.1-8b-instruct",
        "helicone": "gpt-4o-mini",
    }
    return defaults.get(provider, "gpt-4o-mini")


# ----------------------------------------------------------------------------
# Settings helpers
# ----------------------------------------------------------------------------

def mirror_enabled_from_settings(settings_obj) -> bool:
    return bool(getattr(settings_obj, "reflector_enabled", False))


def mirror_auditor_override_from_settings(settings_obj) -> Optional[str]:
    return getattr(settings_obj, "reflector_auditor_model", None) or None
