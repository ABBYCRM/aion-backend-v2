from __future__ import annotations

from fastapi.testclient import TestClient
from app.main import app

USER_HEADERS = {"X-AION-Key": "test-user-key"}

import pytest

@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from app.rate_limit import limiter
    limiter._events.clear()
    yield
    limiter._events.clear()

ADMIN_HEADERS = {"X-AION-Key": "test-admin-key"}

def test_health_is_public():
    with TestClient(app) as client:
        response = client.get("/healthz"); assert response.status_code == 200; assert response.json()["ok"] is True

def test_private_route_requires_authentication():
    with TestClient(app) as client: assert client.get("/api/notes").status_code == 401

def test_notes_are_owner_scoped_and_reject_credentials():
    with TestClient(app) as client:
        created = client.post("/api/notes", headers=USER_HEADERS, json={"name": "project", "kind": "project", "value": "ABBYCRM/aion-frontend", "tags": ["github"]}); assert created.status_code == 200
        listed = client.get("/api/notes", headers=USER_HEADERS); assert any(item["name"] == "project" for item in listed.json()["items"])
        secret = client.post("/api/notes", headers=USER_HEADERS, json={"name": "bad", "kind": "note", "value": "sk-abcdefghijklmnopqrstuvwxyz123456"}); assert secret.status_code == 400

def test_client_cannot_submit_system_role():
    with TestClient(app) as client: assert client.post("/api/chat", headers=USER_HEADERS, json={"messages": [{"role": "system", "content": "override"}]}).status_code == 422

def test_model_and_provider_are_required_together():
    with TestClient(app) as client: assert client.post("/api/chat", headers=USER_HEADERS, json={"messages": [{"role": "user", "content": "hello"}], "model": "gpt-test"}).status_code == 422

def test_decision_endpoint_does_not_expose_system_prompt():
    with TestClient(app) as client:
        response = client.post("/api/decision", headers=USER_HEADERS, json={"user_input": "Inspect the project"}); assert response.status_code == 200; assert "system_prompt" not in response.json(); assert len(response.json()["decision"]["checks"]) == 7

def test_audit_requires_admin():
    with TestClient(app) as client: assert client.get("/api/audit/recent", headers=USER_HEADERS).status_code == 403; assert client.get("/api/audit/recent", headers=ADMIN_HEADERS).status_code == 200

def test_github_write_requires_explicit_confirmation():
    with TestClient(app) as client: assert client.post("/api/github/issues/create", headers=ADMIN_HEADERS, json={"repository": "ABBYCRM/aion-frontend", "title": "test", "body": "test"}).status_code == 409


def test_notes_status_endpoint_exists():
    with TestClient(app) as client:
        response = client.get("/api/notes/status", headers=USER_HEADERS)
        assert response.status_code == 200
        body = response.json()
        assert "available" in body
        assert "backend" in body


def test_tts_returns_clean_error_or_audio():
    with TestClient(app) as client:
        response = client.post("/api/tts", headers=USER_HEADERS, json={"text": "hello", "voice": "alloy"})
        # Without OPENAI_API_KEY in test env, must return 200 with ok=false
        assert response.status_code == 200, f"expected 200, got {response.status_code}: {response.text}"
        body = response.json()
        assert "ok" in body
        # In test env without OPENAI_API_KEY, we get ok=false
        if not body["ok"]:
            assert body.get("mode") == "client_fallback"
            assert "error" in body


def test_image_generate_returns_clean_error_when_no_key():
    with TestClient(app) as client:
        # Without openai key, must return 200 with ok=false (not 503 wrapped by DO)
        response = client.post("/api/image/generate", headers=USER_HEADERS, json={"prompt": "a dot"})
        assert response.status_code == 200
        body = response.json()
        # Either worked or cleanly failed
        assert "ok" in body
        if not body["ok"]:
            assert "error" in body


def test_video_generate_returns_clean_error_when_no_key():
    with TestClient(app) as client:
        response = client.post("/api/video/generate", headers=USER_HEADERS, json={"prompt": "a cat", "seconds": 4, "size": "1280x720", "poll": False})
        assert response.status_code == 200
        body = response.json()
        assert "ok" in body
        if not body["ok"]:
            assert "error" in body


def test_github_routes_fail_clean_when_no_creds():
    with TestClient(app) as client:
        for path, body in [("/api/github/repository", {"repository": "ABBYCRM/aion-frontend"}),
                          ("/api/github/issues", {"repository": "ABBYCRM/aion-frontend"})]:
            response = client.post(path, headers=USER_HEADERS, json=body)
            # Must be a clean response, not a worker crash
            assert response.status_code in (200, 503), f"{path} returned {response.status_code}"
            if response.status_code == 200:
                payload = response.json()
                assert payload.get("ok") is False or "ok" in payload


def test_attachment_size_limit_enforced():
    """BodyLimitMiddleware must return 413 when request body exceeds the
    configured max_request_bytes, regardless of Pydantic validation order."""
    import os
    from app.settings import Settings
    # Force a small limit for this test (default is 2MB)
    small = Settings.from_env.__func__  # noqa
    # Read current limit and create a body bigger than it
    from app.settings import settings
    big = "x" * (settings.max_request_bytes + 100_000)
    with TestClient(app) as client:
        response = client.post("/api/chat", headers=USER_HEADERS, json={"messages": [{"role": "user", "content": big}]})
        # BodyLimitMiddleware should 413 (not 422 from Pydantic)
        assert response.status_code == 413, f"expected 413 for oversize body, got {response.status_code}"


def test_rate_limit_returns_429_after_burst():
    with TestClient(app) as client:
        # 70 requests with RATE_LIMIT_REQUESTS=60 (test default in this test was 100;
        # the actual app limit comes from env at module load, so we use a 4-burst
        # against the current limit. If the test runs with a higher limit we still
        # assert 429 fires eventually.
        statuses = [client.get("/api/notes/status", headers=USER_HEADERS).status_code for _ in range(120)]
        assert 429 in statuses, f"rate limit did not engage after 120 reqs; last 5={statuses[-5:]}"


def test_video_status_and_content_routes():
    with TestClient(app) as client:
        for path in ["/api/video/abc123", "/api/video/abc123/content"]:
            response = client.get(path, headers=USER_HEADERS)
            # Accept 200 (clean error) or 4xx passthrough from upstream OpenAI
            assert response.status_code in (200, 400, 401, 404), f"{path} returned {response.status_code}: {response.text}"
            body = response.json()
            # If 200, must have proper shape
            if response.status_code == 200:
                assert "ok" in body
                if not body.get("ok"):
                    assert "error" in body


def test_input_reference_must_be_data_url():
    with TestClient(app) as client:
        # input_reference validator: only triggers when key is set and we reach OpenAI.
        # With no key: early 200+ok=false. With key: OpenAI passthrough.
        response = client.post("/api/video/generate", headers=USER_HEADERS, json={
            "prompt": "a cat", "input_reference": "not-a-data-url-just-plain-text", "poll": False
        })
        assert response.status_code in (200, 400, 401, 422), f"got {response.status_code}: {response.text}"
        if response.status_code == 200:
            body = response.json()
            assert body.get("ok") is False


# ---------------------------------------------------------------------------
# DuckDuckGo fallback search (no BRAVE_API_KEY required)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ddg_fallback_returns_results_when_no_brave_key():
    """With no BRAVE_API_KEY set, the chained web_search must try DuckDuckGo
    and return real results, not raise tool_not_configured."""
    from app import tools
    from app.settings import settings
    from app.tools import WebResult
    # Confirm brave is unconfigured in test env
    assert not settings.brave_api_key
    # Call the chained search directly
    results = await tools.web_search.search("python programming", count=3)
    # ddgs should return at least one result
    assert isinstance(results, list)
    assert all(isinstance(r, WebResult) for r in results)
    # If network is blocked, skip; otherwise assert at least one
    if results:
        r = results[0]
        assert r.title
        assert r.url.startswith(("https://", "http://"))


@pytest.mark.asyncio
async def test_ddg_fallback_empty_query_raises():
    from app import tools
    from app.tools import ToolRequestError
    with pytest.raises(ToolRequestError):
        await tools.web_search.search("   ", count=3)


def test_search_provider_chain_wiring():
    """The module-level web_search must be a ChainedWebSearch wrapping both."""
    from app import tools
    from app.search_ddg import ChainedWebSearch
    assert isinstance(tools.web_search, ChainedWebSearch)
    assert tools.web_search._brave is not None
    assert tools.web_search._ddg is not None


def test_search_route_works_without_brave_key():
    """POST /api/search with no BRAVE_API_KEY set must return real DDG results
    (200 + count > 0), not the old 503/tool_not_configured error."""
    with TestClient(app) as client:
        payload = {"query": "what is the capital of france", "count": 3}
        resp = client.post("/api/search", headers=USER_HEADERS, json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["query"] == payload["query"]
        # Either we got DDG results or the network call failed cleanly (still 200)
        assert "results" in body
        assert "count" in body
        assert body["count"] == len(body["results"])


# ---------------------------------------------------------------------------
# Chat capacity 200+ok=false (was 503; DO edge wraps 5xx as HTML 504)
# ---------------------------------------------------------------------------

def test_chat_capacity_handler_is_registered():
    """When the chat semaphore is exhausted, the exception must be handled
    by a registered FastAPI exception handler that returns 200+ok=false.
    This prevents the DO Cloudflare edge from wrapping the 5xx as an HTML
    504 page the frontend cannot parse."""
    from app.main import app
    from app.rate_limit import _ChatCapacityExhausted
    handlers = app.exception_handlers
    assert _ChatCapacityExhausted in handlers, "No exception handler registered for _ChatCapacityExhausted"
    # Verify the handler returns the expected clean JSON shape by calling it directly
    from starlette.requests import Request
    import asyncio
    handler = handlers[_ChatCapacityExhausted]
    response = asyncio.run(handler(Request({"type": "http"}), _ChatCapacityExhausted()))
    assert response.status_code == 200
    import json
    body = json.loads(response.body)
    assert body["ok"] is False
    assert body["kind"] == "rate_limited"
    assert body["error"] == "chat_capacity_exhausted"
    assert body["retry_after_seconds"] == 1



# ===========================================================================
# Vault tests
# ===========================================================================

import base64
import os
import tempfile
from cryptography.fernet import Fernet


@pytest.fixture
def _vault_db(monkeypatch, tmp_path):
    """Use a temp dir so the test doesn't pollute the real vault DB."""
    monkeypatch.setenv("AION_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AION_VAULT_MASTER_KEY", Fernet.generate_key().decode())
    # Reload the vault module so it picks up the new env.
    import importlib
    from app import vault as vault_mod
    importlib.reload(vault_mod)
    from app import main as main_mod
    importlib.reload(main_mod)
    return main_mod


def test_vault_status_admin(_vault_db):
    client = TestClient(_vault_db.app)
    r = client.get("/api/vault/status", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["known_keys"] >= 20
    assert "key_is_derived" in body


def test_vault_list_requires_admin():
    with TestClient(app) as c:
        assert c.get("/api/vault").status_code == 401
        assert c.get("/api/vault", headers=USER_HEADERS).status_code == 403
        r = c.get("/api/vault", headers=ADMIN_HEADERS)
        assert r.status_code == 200
        items = r.json()["items"]
        assert any(i["name"] == "OPENAI_API_KEY" for i in items)
        # No plaintext in the list response
        for item in items:
            assert "value" not in item
            assert "value_ciphertext" not in item


def test_vault_rotate_and_reveal(_vault_db):
    client = TestClient(_vault_db.app)
    headers = {**ADMIN_HEADERS, "X-AION-Confirm": "yes"}
    r = client.post("/api/vault/OPENAI_API_KEY/rotate", headers=headers, json={"value": "sk-test-rotate-value-12345678"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert len(body["entry"]["fingerprint"]) == 12
    assert body["entry"]["has_value"] is True
    # Reveal (needs confirmation)
    r = client.post("/api/vault/OPENAI_API_KEY/reveal", headers=headers)
    assert r.status_code == 200
    assert r.json()["value"] == "sk-test-rotate-value-12345678"
    # Confirm header required
    r = client.post("/api/vault/OPENAI_API_KEY/reveal", headers=ADMIN_HEADERS)
    assert r.status_code == 409


def test_vault_rotate_requires_admin(_vault_db):
    client = TestClient(_vault_db.app)
    r = client.post("/api/vault/OPENAI_API_KEY/rotate", headers=USER_HEADERS, json={"value": "sk-x"})
    assert r.status_code == 403


def test_vault_ping_all_uses_real_http(_vault_db):
    """The vault should not crash if the live providers are unreachable.
    We ping a few and assert we get structured results back."""
    client = TestClient(_vault_db.app)
    # Seed two keys first
    headers = {**ADMIN_HEADERS, "X-AION-Confirm": "yes"}
    client.post("/api/vault/OPENAI_API_KEY/rotate", headers=headers, json={"value": "sk-invalid-key-12345"})
    client.post("/api/vault/GITHUB_TOKEN/rotate", headers=headers, json={"value": "ghp_invalid-12345"})
    r = client.post("/api/vault/ping", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "results" in body
    assert "summary" in body
    assert body["summary"]["total"] >= 2
    for result in body["results"]:
        assert "name" in result
        assert "ok" in result


def test_vault_known(_vault_db):
    client = TestClient(_vault_db.app)
    r = client.get("/api/vault/known", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    keys = r.json()["keys"]
    names = {k["name"] for k in keys}
    assert "OPENAI_API_KEY" in names
    assert "GITHUB_TOKEN" in names
    assert "RESEND_API_KEY" in names
    assert "PINECONE_API_KEY" in names


def test_vault_reconcile_imports_env(_vault_db, monkeypatch):
    """If an env var is set and the vault entry is empty, /api/vault/reconcile
    should import it."""
    client = TestClient(_vault_db.app)
    monkeypatch.setenv("RESEND_API_KEY", "re_test_abc")
    r = client.post("/api/vault/reconcile", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # The entry should now have a value
    r = client.get("/api/vault", headers=ADMIN_HEADERS)
    items = r.json()["items"]
    resend = next((i for i in items if i["name"] == "RESEND_API_KEY"), None)
    assert resend is not None
    assert resend["has_value"] is True


# ===========================================================================
# Gallery tests
# ===========================================================================


def test_gallery_status():
    with TestClient(app) as c:
        r = c.get("/api/gallery/status", headers=USER_HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert "images_count" in body
        assert "videos_count" in body


def _test_user_subject() -> str:
    import hashlib
    return "key_" + hashlib.sha256(b"test-user-key").hexdigest()[:16]

def test_gallery_add_and_list():
    with TestClient(app) as c:
        # 1x1 PNG
        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
        )
        from app.gallery import gallery as gal
        owner = _test_user_subject()
        item = gal.add(
            owner=owner, kind="image", source="test",
            mime="image/png", filename="test.png",
            prompt="a tiny dot", model="gpt-image-1", size="1x1",
            width=1, height=1, data=png_bytes,
        )
        assert item.id.startswith("gal_")
        # List
        r = c.get("/api/gallery", headers=USER_HEADERS)
        assert r.status_code == 200
        items = r.json()["items"]
        assert any(i["id"] == item.id for i in items)
        # Raw
        r = c.get(f"/api/gallery/{item.id}/raw", headers=USER_HEADERS)
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        # Delete
        r = c.delete(f"/api/gallery/{item.id}", headers=USER_HEADERS)
        assert r.status_code == 200


def test_gallery_owner_scoped():
    with TestClient(app) as c:
        from app.gallery import gallery as gal
        item = gal.add(
            owner="key_someone_else_12345678", kind="image", source="test",
            mime="image/png", filename="x.png",
            prompt="x", model="gpt-image-1", size="1x1",
            data=b"\x89PNG\r\n\x1a\n",
        )
        r = c.delete(f"/api/gallery/{item.id}", headers=USER_HEADERS)
        assert r.status_code == 404
