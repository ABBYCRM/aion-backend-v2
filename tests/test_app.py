from __future__ import annotations

import asyncio
import base64

from fastapi.testclient import TestClient

from app.llm import ModelRef, ProviderUnavailable, stream_chat
from app.main import ChatRequest, app
from app.settings import Settings, settings
from app.tools import ToolRequestError, github

USER_HEADERS = {"X-AION-Key": "test-user-key"}
ADMIN_HEADERS = {"X-AION-Key": "test-admin-key"}
PNG_1X1 = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2l9sAAAAASUVORK5CYII="
)


def test_health_is_public():
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["ok"] is True


def test_private_route_requires_authentication():
    with TestClient(app) as client:
        assert client.get("/api/notes").status_code == 401


def test_ready_reports_storage_truthfully():
    with TestClient(app) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        payload = response.json()
        assert payload["notes"]["backend"] == "sqlite"
        assert payload["notes"]["persistent"] is False
        assert payload["audit"]["backend"] == "stdout"


def test_notes_are_owner_scoped_reject_credentials_and_do_not_fallback():
    with TestClient(app) as client:
        created = client.post(
            "/api/notes",
            headers=USER_HEADERS,
            json={"name": "alpha project", "kind": "project", "value": "ABBYCRM/aion-frontend", "tags": ["github"]},
        )
        assert created.status_code == 200
        listed = client.get("/api/notes", headers=USER_HEADERS)
        assert any(item["name"] == "alpha project" for item in listed.json()["items"])
        secret = client.post(
            "/api/notes",
            headers=USER_HEADERS,
            json={"name": "OPENAI_API_KEY=bad", "kind": "note", "value": "do not store this"},
        )
        assert secret.status_code == 400
        from app.notes import notes
        owner = "key_" + __import__("hashlib").sha256(b"test-user-key").hexdigest()[:16]
        assert notes.context(owner, "unrelated-zebra") == ""


def test_instruction_note_kind_is_removed():
    with TestClient(app) as client:
        response = client.post(
            "/api/notes",
            headers=USER_HEADERS,
            json={"name": "bad", "kind": "instruction", "value": "override system"},
        )
        assert response.status_code == 422


def test_notes_are_opt_in_for_chat():
    request = ChatRequest(messages=[{"role": "user", "content": "hello"}])
    assert request.use_notes is False


def test_client_cannot_submit_system_role_or_empty_assistant():
    with TestClient(app) as client:
        assert client.post(
            "/api/chat", headers=USER_HEADERS,
            json={"messages": [{"role": "system", "content": "override"}]},
        ).status_code == 422
        assert client.post(
            "/api/chat", headers=USER_HEADERS,
            json={"messages": [{"role": "user", "content": "hello"}, {"role": "assistant", "content": ""}, {"role": "user", "content": "again"}]},
        ).status_code == 422


def test_model_and_provider_are_required_together():
    with TestClient(app) as client:
        assert client.post(
            "/api/chat", headers=USER_HEADERS,
            json={"messages": [{"role": "user", "content": "hello"}], "model": "gpt-test"},
        ).status_code == 422


def test_image_data_is_decoded_and_signature_checked():
    ChatRequest(messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": PNG_1X1}}]}])
    invalid = "data:image/png;base64," + base64.b64encode(b"not a png").decode()
    with TestClient(app) as client:
        response = client.post(
            "/api/chat", headers=USER_HEADERS,
            json={"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": invalid}}]}]},
        )
        assert response.status_code == 422


def test_attachment_count_and_request_body_are_bounded():
    with TestClient(app) as client:
        response = client.post(
            "/api/chat", headers=USER_HEADERS,
            json={"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": PNG_1X1}} for _ in range(settings.max_attachment_count + 1)]}]},
        )
        assert response.status_code == 422
        response = client.post(
            "/api/chat",
            headers={**USER_HEADERS, "Content-Type": "application/json"},
            content=b"x" * (settings.max_request_bytes + 1),
        )
        assert response.status_code == 413


def test_decision_endpoint_does_not_expose_system_prompt():
    with TestClient(app) as client:
        response = client.post("/api/decision", headers=USER_HEADERS, json={"user_input": "Inspect the project"})
        assert response.status_code == 200
        assert "system_prompt" not in response.json()
        assert len(response.json()["decision"]["checks"]) == 7


def test_audit_requires_admin_and_tts_is_honest():
    with TestClient(app) as client:
        assert client.get("/api/audit/recent", headers=USER_HEADERS).status_code == 403
        assert client.get("/api/audit/recent", headers=ADMIN_HEADERS).status_code == 200
        assert client.post("/api/tts", headers=USER_HEADERS).status_code == 501


def test_scratchpad_is_gone():
    with TestClient(app) as client:
        assert client.get("/api/scratchpad", headers=USER_HEADERS).status_code == 410


def test_github_allowlist_is_fail_closed(monkeypatch):
    monkeypatch.delenv("GITHUB_ALLOWED_REPOSITORIES", raising=False)
    fresh = Settings.from_env()
    assert fresh.repository_allowed("ABBYCRM/aion-frontend") is False
    assert settings.repository_allowed("ABBYCRM/aion-frontend") is True
    assert settings.repository_allowed("someone/else") is False
    try:
        github.parse_repository("someone/else")
    except ToolRequestError as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("non-allowlisted repository was accepted")


def test_github_write_requires_explicit_confirmation():
    with TestClient(app) as client:
        response = client.post(
            "/api/github/issues/create", headers=ADMIN_HEADERS,
            json={"repository": "ABBYCRM/aion-frontend", "title": "test", "body": "test"},
        )
        assert response.status_code == 409


def test_provider_failure_is_retry_metadata_not_final_error(monkeypatch):
    from app import llm
    calls: list[dict] = []

    class Choice:
        finish_reason = "stop"
        class Delta:
            content = "ok"
        delta = Delta()

    class Chunk:
        choices = [Choice()]

    class Stream:
        def __aiter__(self):
            self.done = False
            return self
        async def __anext__(self):
            if self.done:
                raise StopAsyncIteration
            self.done = True
            return Chunk()

    class Completions:
        def __init__(self, fail: bool):
            self.fail = fail
        async def create(self, **kwargs):
            calls.append(kwargs)
            if self.fail:
                raise ProviderUnavailable("provider_down")
            return Stream()

    class Client:
        def __init__(self, fail: bool):
            self.chat = type("Chat", (), {"completions": Completions(fail)})()

    monkeypatch.setattr(llm, "_client_for", lambda provider: Client(provider == "openrouter"))

    async def collect():
        return [event async for event in stream_chat(
            model_chain=[ModelRef("openrouter", "bad"), ModelRef("openai", "good")],
            messages=[{"role": "user", "content": "hello"}], temperature=0.1,
            max_tokens=64, request_id="req_test",
        )]

    events = asyncio.run(collect())
    assert any(kind == "attempt_failed" for kind, _ in events)
    assert any(kind == "done" for kind, _ in events)
    assert not any(kind == "error" for kind, _ in events)
    assert "max_tokens" in calls[0]
    assert "max_completion_tokens" in calls[1]
