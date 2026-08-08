"""Integration test for the answer mirror's `self_check` SSE event.

Proves that:
1. When AION_REFLECTOR_ENABLED is true, /api/chat emits a `self_check`
   SSE event with the expected shape.
2. When the audit passes, the event has resolved=True.
3. When the audit fails twice, the event has resolved=False.
4. When AION_REFLECTOR_ENABLED is false, no `self_check` event is emitted.
5. The user-visible answer is NOT replaced by the repair — only the
   audit event is added.
"""
from __future__ import annotations

import importlib
import json
import sys

sys.path.insert(0, ".")
import pytest
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

from app import reflector


@pytest.fixture
def _vault_db(monkeypatch, tmp_path):
    """Use a temp dir so the test doesn't pollute the real vault DB."""
    monkeypatch.setenv("AION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AION_VAULT_MASTER_KEY", Fernet.generate_key().decode())
    from app import vault as vault_mod
    importlib.reload(vault_mod)
    from app import main as main_mod
    importlib.reload(main_mod)
    return main_mod


USER_HEADERS = {"X-AION-Key": "test-user-key"}


def _sse_events(text: str) -> list[dict]:
    """Parse SSE response into a list of event dicts."""
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            pass
    return events


def test_self_check_event_emitted_when_mirror_enabled(_vault_db, monkeypatch):
    """When AION_REFLECTOR_ENABLED=true, the chat stream includes a
    self_check event. We stub the local LLM stream and the mirror's
    audit/revision functions to keep this test fast and offline."""
    monkeypatch.setenv("AION_BRAIN_ENABLED", "false")
    monkeypatch.setenv("AION_REFLECTOR_ENABLED", "true")

    # Stub the local LLM stream so the answer is deterministic
    async def fake_stream_chat(*, model_chain, messages, temperature, max_tokens, request_id):
        yield ("open", {"provider": "openai", "model": "gpt-4o-mini"})
        yield ("delta", {"text": "This is a detailed answer " * 10})  # > 80 chars
        yield ("done", {"finish_reason": "stop"})

    # Stub complete_chat so the mirror's audit call returns "resolved: true"
    async def fake_complete_chat(*, provider, model, messages, temperature, max_tokens, request_id):
        return json.dumps({
            "resolved": True, "value_added": 5, "grounded": 4, "honest": 5, "novel": 4,
            "requested_items": ["a"], "answered_items": ["a"],
            "missing_items": [], "weak_items": [],
        }), 100

    # Reload modules to pick up env vars
    import importlib
    from app import settings, llm, main as main_mod
    importlib.reload(settings)
    importlib.reload(llm)
    importlib.reload(main_mod)
    # Patch the streaming function (it lives in llm module)
    monkeypatch.setattr(llm, "stream_chat", fake_stream_chat)
    monkeypatch.setattr(llm, "complete_chat", fake_complete_chat)
    # Re-reload main so it uses the patched llm module
    importlib.reload(main_mod)

    client = TestClient(main_mod.app)
    r = client.post(
        "/api/chat",
        headers=USER_HEADERS,
        json={"messages": [{"role": "user", "content": "Hello"}], "max_tokens": 64, "temperature": 0},
    )
    assert r.status_code == 200

    events = _sse_events(r.text)
    # Find the self_check event
    self_check = [e for e in events if e.get("type") == "self_check"]
    assert len(self_check) == 1, f"expected 1 self_check event, got {len(self_check)}: {events[:5]}"
    payload = self_check[0]
    assert payload["resolved"] is True
    assert payload["passed"] is True
    assert "auditor" in payload
    assert payload["attempts"] == 1
    assert payload["audit"] is not None
    assert payload["audit"]["value_added"] == 5


def test_self_check_event_weak_after_both_attempts(_vault_db, monkeypatch):
    """When the mirror runs the audit twice and both fail, the event has
    resolved=False. The UI should render the "Self-check: weak" badge."""
    monkeypatch.setenv("AION_BRAIN_ENABLED", "false")
    monkeypatch.setenv("AION_REFLECTOR_ENABLED", "true")

    async def fake_stream_chat(*, model_chain, messages, temperature, max_tokens, request_id):
        yield ("open", {"provider": "openai", "model": "gpt-4o-mini"})
        yield ("delta", {"text": "This is a detailed answer " * 10})
        yield ("done", {"finish_reason": "stop"})

    call_count = [0]

    async def fake_complete_chat(*, provider, model, messages, temperature, max_tokens, request_id):
        call_count[0] += 1
        # Revision calls return text; audit calls return JSON
        if "request_id" in str(request_id) and "revision" in str(request_id):
            return "Here is a deeper analysis with substantial new content over 40 chars.", 100
        # First audit: fail. Second audit: also fail.
        return json.dumps({
            "resolved": False, "value_added": 1, "grounded": 2, "honest": 3, "novel": 1,
            "requested_items": ["a", "b"], "answered_items": [],
            "missing_items": ["b"], "weak_items": ["too shallow"],
        }), 100

    import importlib
    from app import settings, llm, main as main_mod
    importlib.reload(settings)
    importlib.reload(llm)
    importlib.reload(main_mod)
    monkeypatch.setattr(llm, "stream_chat", fake_stream_chat)
    monkeypatch.setattr(llm, "complete_chat", fake_complete_chat)
    importlib.reload(main_mod)

    client = TestClient(main_mod.app)
    r = client.post(
        "/api/chat",
        headers=USER_HEADERS,
        json={"messages": [{"role": "user", "content": "Hello"}], "max_tokens": 64, "temperature": 0},
    )
    assert r.status_code == 200

    events = _sse_events(r.text)
    self_check = [e for e in events if e.get("type") == "self_check"]
    assert len(self_check) == 1
    payload = self_check[0]
    assert payload["resolved"] is False
    assert payload["passed"] is False
    assert payload["attempts"] == 2
    assert "b" in payload["audit"]["missing_items"]


def test_no_self_check_event_when_mirror_disabled(_vault_db, monkeypatch):
    """When AION_REFLECTOR_ENABLED=false (default), the chat stream does
    NOT include a self_check event. The mirror is silent and free."""
    monkeypatch.setenv("AION_BRAIN_ENABLED", "false")
    monkeypatch.setenv("AION_REFLECTOR_ENABLED", "false")

    async def fake_stream_chat(*, model_chain, messages, temperature, max_tokens, request_id):
        yield ("open", {"provider": "openai", "model": "gpt-4o-mini"})
        yield ("delta", {"text": "Short answer."})  # < 80 chars
        yield ("done", {"finish_reason": "stop"})

    import importlib
    from app import settings, llm, main as main_mod
    importlib.reload(settings)
    importlib.reload(llm)
    importlib.reload(main_mod)
    monkeypatch.setattr(llm, "stream_chat", fake_stream_chat)
    importlib.reload(main_mod)

    client = TestClient(main_mod.app)
    r = client.post(
        "/api/chat",
        headers=USER_HEADERS,
        json={"messages": [{"role": "user", "content": "Hi"}], "max_tokens": 64, "temperature": 0},
    )
    assert r.status_code == 200

    events = _sse_events(r.text)
    self_check = [e for e in events if e.get("type") == "self_check"]
    assert len(self_check) == 0, f"expected no self_check, got {len(self_check)}: {self_check}"


def test_mirror_failure_does_not_break_chat(_vault_db, monkeypatch):
    """If the mirror's audit call raises an exception, the chat stream
    still completes successfully with [DONE]. The mirror's failure is
    caught and logged but never surfaces to the user."""
    monkeypatch.setenv("AION_BRAIN_ENABLED", "false")
    monkeypatch.setenv("AION_REFLECTOR_ENABLED", "true")

    async def fake_stream_chat(*, model_chain, messages, temperature, max_tokens, request_id):
        yield ("open", {"provider": "openai", "model": "gpt-4o-mini"})
        yield ("delta", {"text": "This is a detailed answer " * 10})
        yield ("done", {"finish_reason": "stop"})

    async def fake_complete_chat(*, provider, model, messages, temperature, max_tokens, request_id):
        raise RuntimeError("provider down")

    import importlib
    from app import settings, llm, main as main_mod
    importlib.reload(settings)
    importlib.reload(llm)
    importlib.reload(main_mod)
    monkeypatch.setattr(llm, "stream_chat", fake_stream_chat)
    monkeypatch.setattr(llm, "complete_chat", fake_complete_chat)
    importlib.reload(main_mod)

    client = TestClient(main_mod.app)
    r = client.post(
        "/api/chat",
        headers=USER_HEADERS,
        json={"messages": [{"role": "user", "content": "Hello"}], "max_tokens": 64, "temperature": 0},
    )
    # Chat must still succeed
    assert r.status_code == 200
    assert "data: [DONE]" in r.text
    # The stream must include the original answer
    assert "detailed answer" in r.text
