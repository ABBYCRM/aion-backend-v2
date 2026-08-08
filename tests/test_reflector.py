"""Tests for the answer mirror (reflector.py)."""
from __future__ import annotations

import json
import sys
import pytest

sys.path.insert(0, ".")
from app import reflector


# ----------------------------------------------------------------------------
# parse_audit
# ----------------------------------------------------------------------------

def test_parse_audit_valid():
    raw = json.dumps({
        "resolved": True,
        "value_added": 5,
        "grounded": 4,
        "honest": 5,
        "novel": 4,
        "requested_items": ["a"],
        "answered_items": ["a"],
        "missing_items": [],
        "weak_items": [],
    })
    audit = reflector.parse_audit(raw)
    assert audit["resolved"] is True
    assert audit["value_added"] == 5
    assert audit["missing_items"] == []


def test_parse_audit_broken_json_fails_closed():
    audit = reflector.parse_audit("not json at all")
    assert audit["resolved"] is False
    assert any("parse_error" in item for item in audit["missing_items"])


def test_parse_audit_contradictory_overrides_to_false():
    raw = json.dumps({
        "resolved": True,  # contradicts the missing_items below
        "value_added": 5,
        "grounded": 4,
        "honest": 5,
        "novel": 4,
        "requested_items": ["a", "b"],
        "answered_items": ["a"],
        "missing_items": ["b"],
        "weak_items": [],
    })
    audit = reflector.parse_audit(raw)
    # Defensive: resolved must be false when missing_items is non-empty
    assert audit["resolved"] is False


def test_parse_audit_weak_items_also_force_unresolved():
    raw = json.dumps({
        "resolved": True,
        "value_added": 5,
        "grounded": 4,
        "honest": 5,
        "novel": 4,
        "requested_items": ["a"],
        "answered_items": ["a"],
        "missing_items": [],
        "weak_items": ["superficial"],
    })
    audit = reflector.parse_audit(raw)
    assert audit["resolved"] is False


def test_parse_audit_out_of_range_int_fails_closed():
    raw = json.dumps({
        "resolved": True,
        "value_added": 7,  # out of range
        "grounded": 4,
        "honest": 5,
        "novel": 4,
        "requested_items": [],
        "answered_items": [],
        "missing_items": [],
        "weak_items": [],
    })
    audit = reflector.parse_audit(raw)
    assert audit["resolved"] is False
    assert any("value_added" in item for item in audit["missing_items"])


def test_parse_audit_strips_markdown_fences():
    raw = "```json\n" + json.dumps({
        "resolved": True,
        "value_added": 3,
        "grounded": 3,
        "honest": 3,
        "novel": 3,
        "requested_items": [],
        "answered_items": [],
        "missing_items": [],
        "weak_items": [],
    }) + "\n```"
    audit = reflector.parse_audit(raw)
    assert audit["resolved"] is True


def test_parse_audit_wrong_type_for_list_fails_closed():
    raw = json.dumps({
        "resolved": True,
        "value_added": 5,
        "grounded": 4,
        "honest": 5,
        "novel": 4,
        "requested_items": "should be a list, not a string",
        "answered_items": [],
        "missing_items": [],
        "weak_items": [],
    })
    audit = reflector.parse_audit(raw)
    assert audit["resolved"] is False


def test_parse_audit_resolved_must_be_bool():
    raw = json.dumps({
        "resolved": "true",  # string, not bool
        "value_added": 5,
        "grounded": 4,
        "honest": 5,
        "novel": 4,
        "requested_items": [],
        "answered_items": [],
        "missing_items": [],
        "weak_items": [],
    })
    audit = reflector.parse_audit(raw)
    assert audit["resolved"] is False


# ----------------------------------------------------------------------------
# estimate_tokens
# ----------------------------------------------------------------------------

def test_estimate_tokens_basic():
    # ~4 chars per token, min 1
    assert reflector.estimate_tokens("") == 1
    assert reflector.estimate_tokens("hi") == 1
    assert reflector.estimate_tokens("hello world this is a test") >= 5
    assert reflector.estimate_tokens("x" * 400) >= 100


# ----------------------------------------------------------------------------
# Auditor routing
# ----------------------------------------------------------------------------

def test_auditor_routing_different_providers():
    # Cheap answer → frontier audit
    assert reflector.AUDITOR_FOR_ANSWER["openai"] == "anthropic"
    assert reflector.AUDITOR_FOR_ANSWER["anthropic"] == "openai"
    assert reflector.AUDITOR_FOR_ANSWER["moonshot"] == "openai"
    # Unknown provider falls back to itself
    assert reflector.AUDITOR_FOR_ANSWER.get("unknown_provider", "unknown_provider") == "unknown_provider"


def test_pick_auditor_model_known_providers():
    assert reflector._pick_auditor_model("openai") == "gpt-4o-mini"
    assert reflector._pick_auditor_model("anthropic") == "claude-3-5-haiku-20241022"


# ----------------------------------------------------------------------------
# run_mirror
# ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_mirror_disabled_returns_immediately():
    result = await reflector.run_mirror(
        answer="This is a perfectly fine answer.",
        user_request="What is X?",
        answer_provider="openai", answer_model="gpt-4o-mini",
        original_input_tokens=100,
        call_audit_fn=_fake_audit_pass, call_revision_fn=_fake_revision_pass,
        enabled=False,
    )
    assert result.resolved is False
    assert result.skip_reason == "disabled"
    assert result.tokens_added == 0


@pytest.mark.asyncio
async def test_run_mirror_empty_input_returns_immediately():
    result = await reflector.run_mirror(
        answer="",
        user_request="What is X?",
        answer_provider="openai", answer_model="gpt-4o-mini",
        original_input_tokens=100,
        call_audit_fn=_fake_audit_pass, call_revision_fn=_fake_revision_pass,
        enabled=True,
    )
    assert result.skip_reason == "empty_input"


@pytest.mark.asyncio
async def test_run_mirror_short_answer_skips_audit():
    short_answer = "OK."  # < 80 chars
    result = await reflector.run_mirror(
        answer=short_answer, user_request="Hello",
        answer_provider="openai", answer_model="gpt-4o-mini",
        original_input_tokens=10,
        call_audit_fn=_fake_audit_pass, call_revision_fn=_fake_revision_pass,
        enabled=True,
    )
    assert result.skip_reason == "too_short"
    assert result.resolved is True


@pytest.mark.asyncio
async def test_run_mirror_passes_on_first_audit():
    result = await reflector.run_mirror(
        answer=_long_answer(),
        user_request="Explain X in detail",
        answer_provider="openai", answer_model="gpt-4o-mini",
        original_input_tokens=100,
        call_audit_fn=_fake_audit_pass, call_revision_fn=_fake_revision_pass,
        enabled=True,
    )
    assert result.resolved is True
    assert result.passed is True
    assert result.attempts == 1
    assert result.repair_text == ""
    assert len(result.audits) == 1


@pytest.mark.asyncio
async def test_run_mirror_repairs_when_first_audit_fails():
    # The audit call: first call fails, second call passes.
    # This simulates "the answer was weak but a repair attempt was stronger."
    audit_call_count = [0]

    async def audit_fn(provider, model, messages, max_tokens, temperature):
        audit_call_count[0] += 1
        # First audit fails, second passes
        if audit_call_count[0] == 1:
            return _audit_response(resolved=False, value_added=1, missing=["deep analysis"]), 200
        return _audit_response(resolved=True, value_added=5, missing=[]), 200

    async def revision_fn(provider, model, messages, max_tokens, temperature):
        return "Here is a deeper analysis with new content.", 100

    result = await reflector.run_mirror(
        answer=_long_answer(),
        user_request="Explain X in detail",
        answer_provider="openai", answer_model="gpt-4o-mini",
        original_input_tokens=100,
        call_audit_fn=audit_fn, call_revision_fn=revision_fn,
        enabled=True,
    )
    # First audit failed, repair was stronger → resolved=True
    assert result.resolved is True
    assert result.attempts == 2
    assert result.repair_text == "Here is a deeper analysis with new content."
    assert len(result.audits) == 2


@pytest.mark.asyncio
async def test_run_mirror_weak_after_both_attempts():
    async def audit_fn(provider, model, messages, max_tokens, temperature):
        return _audit_response(resolved=False, value_added=1, missing=["x"]), 200

    async def revision_fn(provider, model, messages, max_tokens, temperature):
        return "Still a weak answer that is longer than forty characters to pass the gate.", 100

    result = await reflector.run_mirror(
        answer=_long_answer(),
        user_request="Explain X in detail",
        answer_provider="openai", answer_model="gpt-4o-mini",
        original_input_tokens=100,
        call_audit_fn=audit_fn, call_revision_fn=revision_fn,
        enabled=True,
    )
    assert result.resolved is False
    assert result.passed is False
    assert result.attempts == 2
    assert result.repair_text == "Still a weak answer that is longer than forty characters to pass the gate."
    assert len(result.audits) == 2


@pytest.mark.asyncio
async def test_run_mirror_detects_user_knew_already():
    # Long user prompt (user did their own analysis) + audit says value_added=1
    # The mirror should set user_knew_already=True
    long_user_prompt = "x" * 2400  # > 2000 chars = > 500 tokens triggers the check

    async def audit_fn(provider, model, messages, max_tokens, temperature):
        return _audit_response(resolved=False, value_added=1, missing=["new perspective"]), 200

    async def revision_fn(provider, model, messages, max_tokens, temperature):
        return "x", 1

    result = await reflector.run_mirror(
        answer=_long_answer(),
        user_request=long_user_prompt,
        answer_provider="openai", answer_model="gpt-4o-mini",
        original_input_tokens=600,
        call_audit_fn=audit_fn, call_revision_fn=revision_fn,
        enabled=True,
    )
    assert result.user_knew_already is True


@pytest.mark.asyncio
async def test_run_mirror_audit_call_exception_does_not_crash():
    async def audit_fn(provider, model, messages, max_tokens, temperature):
        raise RuntimeError("provider down")

    async def revision_fn(provider, model, messages, max_tokens, temperature):
        return "x", 1

    # Should not raise — the mirror must NEVER break the user-visible response
    result = await reflector.run_mirror(
        answer=_long_answer(),
        user_request="Explain X",
        answer_provider="openai", answer_model="gpt-4o-mini",
        original_input_tokens=100,
        call_audit_fn=audit_fn, call_revision_fn=revision_fn,
        enabled=True,
    )
    assert result.resolved is False
    # No audits recorded because the call failed before parse
    assert result.audits == [] or all("parse_error" in str(a) or a.get("resolved") is False for a in result.audits)


@pytest.mark.asyncio
async def test_run_mirror_revision_call_exception_does_not_crash():
    async def audit_fn(provider, model, messages, max_tokens, temperature):
        return _audit_response(resolved=False, value_added=1, missing=["x"]), 200

    async def revision_fn(provider, model, messages, max_tokens, temperature):
        raise RuntimeError("revision provider down")

    result = await reflector.run_mirror(
        answer=_long_answer(),
        user_request="Explain X",
        answer_provider="openai", answer_model="gpt-4o-mini",
        original_input_tokens=100,
        call_audit_fn=audit_fn, call_revision_fn=revision_fn,
        enabled=True,
    )
    # The audit ran and failed, the revision failed, the mirror completes gracefully
    assert result.resolved is False
    assert result.attempts == 1  # revision never completed


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _long_answer() -> str:
    return (
        "Here is my detailed analysis. " * 20
    )  # ~600 chars, plenty of room for audit


def _audit_response(*, resolved: bool, value_added: int, missing: list[str]) -> str:
    return json.dumps({
        "resolved": resolved,
        "value_added": value_added,
        "grounded": 3,
        "honest": 4,
        "novel": 3,
        "requested_items": ["explain X"],
        "answered_items": [] if not resolved else ["explain X"],
        "missing_items": missing,
        "weak_items": [],
    })


async def _fake_audit_pass(provider, model, messages, max_tokens, temperature):
    return _audit_response(resolved=True, value_added=5, missing=[]), 200


async def _fake_revision_pass(provider, model, messages, max_tokens, temperature):
    return "revised content", 100
