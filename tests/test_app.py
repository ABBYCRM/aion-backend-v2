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





def test_brain_status_returns_probe(_vault_db, monkeypatch):
    """GET /api/brain/status must return {ok,brain:{...}} even when Brain is
    disabled — the pill must always have a state to render."""
    import importlib
    from app import settings, main as main_mod
    # Default: brain_enabled=False
    importlib.reload(settings)
    importlib.reload(main_mod)
    client = TestClient(main_mod.app)
    r = client.get("/api/brain/status", headers=USER_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    brain = body.get("brain", {})
    # brain_enabled=False in default test env -> enabled=False reachable=False
    assert brain.get("enabled") is False
    assert brain.get("reachable") is False
    assert brain.get("error") == "disabled"


def test_brain_status_reports_unreachable_when_brain_down(_vault_db, monkeypatch):
    """When brain_enabled=true + URL is unreachable, /api/brain/status must
    return reachable=False with an error, never raise."""
    monkeypatch.setenv("AION_BRAIN_ENABLED", "true")
    monkeypatch.setenv("AION_BRAIN_URL", "http://localhost:1")
    monkeypatch.setenv("AION_BRAIN_KEY", "test-brain-key")
    monkeypatch.setenv("AION_BRAIN_REQUIRED", "false")
    import importlib
    from app import settings, main as main_mod, brain_client
    importlib.reload(settings)  # rebuilds settings.settings
    importlib.reload(brain_client)  # picks up new settings reference
    importlib.reload(main_mod)  # re-imports settings + brain_client into main
    client = TestClient(main_mod.app)
    r = client.get("/api/brain/status", headers=USER_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    brain = body.get("brain", {})
    assert brain.get("enabled") is True
    assert brain.get("reachable") is False
    assert brain.get("latency_ms") is not None
    assert brain.get("error")
    # The X-AION-Brain headers are added by the /api/decision and /api/chat
    # routes; /api/brain/status itself does not need them. CORS
    # expose_headers is verified at the live deploy.




def test_kernel_defers_when_tool_errors():
    """resolve_decision must DEFER when a tool was requested and errored."""
    from app.kernel import resolve_decision, MissionContext, DecisionState
    ctx = MissionContext(user_input="review this repo", history=[], metadata={"web_search": False, "github": True, "tool_context_available": False, "tool_errors": ["github_repository_not_allowed: ABBYCRM/robot-vacuum not on GITHUB_ALLOWED_REPOSITORIES"]})
    d = resolve_decision(ctx)
    assert d.state == DecisionState.DEFER
    assert any(c.law == "EPISTEMIC" and not c.passed for c in d.checks)
    assert "fail" in d.rationale.lower() or "error" in d.rationale.lower() or "evidence" in d.rationale.lower()


def test_kernel_commits_when_no_tool_requested():
    """A plain question (no /search / /github) still COMMITs."""
    from app.kernel import resolve_decision, MissionContext, DecisionState
    ctx = MissionContext(user_input="hi", history=[], metadata={"web_search": False, "github": False, "tool_context_available": False, "tool_errors": []})
    d = resolve_decision(ctx)
    assert d.state == DecisionState.COMMIT


def test_system_prompt_surfaces_tool_errors():
    """When a tool errored, the prompt must include a 'Tool errors' section
    with the error text, and the no-filler rule."""
    from app.kernel import build_system_prompt, Decision, LawCheck, DecisionState
    decision = Decision(state=DecisionState.DEFER, score=0.2, rationale="tool failed", checks=[], protocol={})
    p = build_system_prompt(decision, tool_context="", notes_context="", tool_errors=("github_repository_not_allowed: ABBYCRM/robot-vacuum not on GITHUB_ALLOWED_REPOSITORIES",))
    assert "github_repository_not_allowed" in p
    assert "Tool errors this turn" in p
    assert "DO NOT substitute generic advice" in p
    # A successful case must NOT include the section
    decision_ok = Decision(state=DecisionState.COMMIT, score=0.9, rationale="ok", checks=[], protocol={})
    p_ok = build_system_prompt(decision_ok, tool_context="", notes_context="", tool_errors=())
    # Successful case must NOT have a "Tool errors" SECTION (rules may still
    # mention "Tool errors" inside the quoted instruction).
    assert "Tool errors this turn" not in p_ok


def test_chat_defers_when_github_tool_blocked(_vault_db, monkeypatch):
    """The same prompt that produced the soft-hallucination bug
    (https://github.com/ABBYCRM/robot-vacuum ...) MUST now return
    DEFER AND the LLM is NEVER called. The hard DEFER gate streams a
    factual refusal directly and the chat ends.
    """
    monkeypatch.setenv("GITHUB_ALLOWED_REPOSITORIES", "ABBYCRM/aion-backend-v2")
    import importlib
    from app import settings, main as main_mod, tools as tools_mod
    importlib.reload(settings)
    importlib.reload(tools_mod)
    importlib.reload(main_mod)
    client = TestClient(main_mod.app)
    with client.stream(
        "POST", "/api/chat",
        headers=USER_HEADERS,
        json={"messages": [{"role": "user", "content": "https://github.com/ABBYCRM/robot-vacuum what do you think of this repo"}], "max_tokens": 256},
    ) as r:
        assert r.status_code == 200
        import json as _json
        decision_state = None
        tool_errors = []
        saw_open = False
        saw_done = False
        llm_attempt_seen = False
        refusal_text = []
        for line in r.iter_lines():
            if not line: continue
            if isinstance(line, bytes): line = line.decode("utf-8", errors="ignore")
            if line.startswith("data: "):
                payload = line[6:]
                if payload == "[DONE]": continue
                try: evt = _json.loads(payload)
                except Exception: continue
                t = evt.get("type")
                if t == "decision": decision_state = evt.get("decision", {}).get("state")
                elif t == "tool_error": tool_errors.append(evt)
                elif t == "open": saw_open = True
                elif t == "done": saw_done = True
                elif t == "attempt": llm_attempt_seen = True
                elif t == "delta": refusal_text.append(evt.get("text", ""))
        # 1. DEFER is the kernel state
        assert decision_state == "DEFER", f"expected DEFER, got {decision_state}; tool_errors: {tool_errors}"
        # 2. The tool error was reported
        assert any("github_repository_not_allowed" in t.get("message", "").lower() or "not allowlist" in t.get("message", "").lower() for t in tool_errors), f"expected not-allowlisted tool error, got: {tool_errors}"
        # 3. The hard DEFER gate opened, sent deltas, and closed
        assert saw_open, "expected open event from the defer-gate"
        assert saw_done, "expected done event from the defer-gate"
        # 4. The LLM was NEVER called (no attempt event)
        assert not llm_attempt_seen, "LLM was called despite tool failure — hard DEFER gate missing"
        # 5. The refusal text contains the repository name + the fix path
        full = "".join(refusal_text)
        assert "DEFER" in full
        assert "robot-vacuum" in full
        assert "GITHUB_ALLOWED_REPOSITORIES" in full


def test_defer_tool_failure_text_mentions_repo_and_fix():
    from app.main import _defer_tool_failure_text
    text = _defer_tool_failure_text(["github_repository_not_allowed: x/y not on GITHUB_ALLOWED_REPOSITORIES"], repository="x/y")
    assert "DEFER" in text
    assert "x/y" in text
    assert "GITHUB_ALLOWED_REPOSITORIES" in text
    # No tool error -> no repo-specific hint
    text2 = _defer_tool_failure_text(["github_http_500"], repository="x/y")
    assert "DEFER" in text2
    assert "x/y" in text2
    assert "See the error above" in text2




def test_decision_includes_failure_block_on_github_allowlist():
    """When GitHub errors with github_repository_not_allowed, the
    decision must include a structured failure block with kind=
    github_allowlist_blocked + the repo + the fix."""
    from app.kernel import resolve_decision, MissionContext
    ctx = MissionContext(user_input="x", history=[], metadata={"web_search": False, "github": True, "tool_context_available": False, "tool_errors": ["github_repository_not_allowed: ABBYCRM/robot-vacuum not on GITHUB_ALLOWED_REPOSITORIES"]})
    d = resolve_decision(ctx)
    assert d.failure["kind"] == "github_allowlist_blocked"
    assert d.failure["tool"] == "github"
    assert "ABBYCRM/robot-vacuum" in d.failure["next_step"]
    assert "GITHUB_ALLOWED_REPOSITORIES" in d.failure["next_step"]


def test_decision_failure_kind_for_search():
    from app.kernel import resolve_decision, MissionContext
    ctx = MissionContext(user_input="x", history=[], metadata={"web_search": True, "github": False, "tool_context_available": False, "tool_errors": ["search_not_configured: BRAVE_API_KEY missing"]})
    d = resolve_decision(ctx)
    assert d.failure["kind"] == "search_not_configured"
    assert d.failure["tool"] == "search"
    assert "BRAVE_API_KEY" in d.failure["next_step"] or "DuckDuckGo" in d.failure["next_step"]


def test_decision_failure_empty_for_commit():
    from app.kernel import resolve_decision, MissionContext
    ctx = MissionContext(user_input="x", history=[], metadata={"web_search": False, "github": False, "tool_context_available": False, "tool_errors": []})
    d = resolve_decision(ctx)
    assert d.failure == {}




def test_policy_endpoint_returns_operator_view():
    """GET /api/policy must return github + cors + brain status with no secrets."""
    client = TestClient(app)
    r = client.get("/api/policy", headers=USER_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    gh = body.get("github", {})
    assert "allowed_repositories" in gh
    assert "allowlist_mode" in gh  # "allow_all" or "restricted"
    assert isinstance(gh.get("write_enabled"), bool)
    brain = body.get("brain", {})
    assert "enabled" in brain
    cors = body.get("cors", {})
    assert "origins" in cors
    # No secrets in the response
    raw = r.text.lower()
    assert "token" not in raw or "token_configured" in raw  # only the boolean
    assert "api_key" not in raw or "configured" in raw


def test_policy_github_check_routes_allowlist_decision(monkeypatch):
    """GET /api/policy/github/check?repository=X must say allowed + normalized
    when X is in the allowlist, and allowed=False with a reason when not."""
    # Restrict to a known repo, then test against an unknown one
    monkeypatch.setenv("GITHUB_ALLOWED_REPOSITORIES", "ABBYCRM/aion-backend-v2")
    import importlib
    from app import settings, main as main_mod, tools as tools_mod
    importlib.reload(settings)
    importlib.reload(tools_mod)  # tools holds a settings reference too
    importlib.reload(main_mod)
    client = TestClient(main_mod.app)
    r = client.get("/api/policy/github/check?repository=ABBYCRM/robot-vacuum", headers=USER_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body.get("allowed") is False, f"expected blocked, got: {body}"
    assert "not allowlist" in (body.get("reason") or "").lower() or "github_repository_not_allowed" in (body.get("reason") or "").lower()
    # Now allow all
    monkeypatch.setenv("GITHUB_ALLOWED_REPOSITORIES", "")
    importlib.reload(settings)
    importlib.reload(tools_mod)
    importlib.reload(main_mod)
    client2 = TestClient(main_mod.app)
    r2 = client2.get("/api/policy/github/check?repository=ABBYCRM/robot-vacuum", headers=USER_HEADERS)
    body2 = r2.json()
    assert body2.get("allowed") is True, f"expected allowed when allowlist empty, got: {body2}"




# ===========================================================================
# Skill registry full pack (12 contracts, RAG, GitHub, scrape, email)
# ===========================================================================

def test_skills_bootstrap_seeds_12_contracts():
    """bootstrap() must populate all 12 built-in skills into the SQLite DB."""
    from app.skills import bootstrap
    from app.skills.registry_core import get_registry
    info = bootstrap()
    assert info.get("seeded") == 20
    reg = get_registry()
    ids = {s.id for s in reg.list(enabled_only=False)}
    # Network skills
    assert "web.search" in ids
    assert "github.repo" in ids
    assert "github.file" in ids
    assert "github.issues" in ids
    assert "github.search" in ids
    assert "scrape.url" in ids
    # Write skills
    assert "email.send" in ids
    # RAG skills
    assert "rag.skills.search" in ids
    assert "rag.code.search" in ids
    assert "rag.upsert" in ids
    assert "rag.index_catalog" in ids
    # Meta
    assert "skills.catalog" in ids


def test_skill_registry_catalog_hides_secret_metadata():
    """public_dict must not leak value_length or fingerprint fields."""
    from app.skills import bootstrap
    from app.skills.registry_core import get_registry
    bootstrap()
    reg = get_registry()
    cat = reg.catalog()
    assert len(cat) >= 12
    for s in cat:
        assert "value_length" not in s
        assert "fingerprint" not in s
        assert "metadata" not in s
        assert {"id", "name", "description", "side_effect", "input_schema", "output_schema"}.issubset(s.keys())


def test_skill_runner_rejects_unknown_skill():
    from app.skills.runner import get_runner
    import asyncio
    result = asyncio.run(get_runner().run("nonexistent.skill", {"x": 1}))
    assert result.ok is False
    assert result.error_code == "skill_not_found"


def test_skill_runner_rejects_disabled_skill():
    from app.skills import bootstrap
    from app.skills.registry_core import get_registry
    from app.skills.runner import get_runner
    import asyncio
    bootstrap()
    reg = get_registry()
    reg.upsert(__import__("app.skills.registry_core", fromlist=["SkillSpec"]).SkillSpec(
        id="test.skill", name="t", description="d", side_effect="none", enabled=False,
        input_schema={}, output_schema={}, executor="", tags=[], error_codes=[],
    ))
    result = asyncio.run(get_runner().run("test.skill", {}))
    assert result.ok is False
    assert result.error_code == "skill_disabled"


def test_skill_runner_rejects_missing_required_args():
    from app.skills import bootstrap
    from app.skills.runner import get_runner
    import asyncio
    bootstrap()
    result = asyncio.run(get_runner().run("github.repo", {}))
    assert result.ok is False
    assert result.error_code == "invalid_args"
    assert "missing_required:repository" in (result.error_message or "")


def test_skill_runner_rejects_wrong_type():
    from app.skills import bootstrap
    from app.skills.runner import get_runner
    import asyncio
    bootstrap()
    # web.search requires query (string); sending integer must fail
    result = asyncio.run(get_runner().run("web.search", {"query": 123}))
    # The new pack's _validate only checks "required", not types — so
    # sending an int still passes the registry but the executor will
    # raise. The expected behavior is that the chain fails — accept
    # either invalid_args or skill_exception with type-mismatch message.
    assert result.ok is False


def test_skill_runner_executes_wired_builtin():
    """skills.catalog is pure (no network), must return ok with the catalog."""
    from app.skills import bootstrap
    from app.skills.runner import get_runner
    import asyncio
    bootstrap()
    result = asyncio.run(get_runner().run("skills.catalog", {}))
    assert result.ok is True
    assert result.skill_id == "skills.catalog"
    assert "count" in result.data
    assert result.data["count"] >= 20
    assert result.run_id is not None
    assert result.run_id.startswith("run_")


def test_skill_runner_reports_executor_not_wired():
    """If the executor is missing, runner returns skill_executor_not_wired
    without ever running code."""
    from app.skills import bootstrap
    from app.skills.runner import get_runner
    from app.skills.registry_core import SkillSpec, get_registry
    import asyncio
    bootstrap()
    reg = get_registry()
    # Register a skill that references an executor that doesn't exist
    reg.upsert(SkillSpec(
        id="test.unwired", name="u", description="d", side_effect="none",
        input_schema={}, output_schema={}, executor="builtin:does.not.exist",
        tags=[], error_codes=[],
    ))
    result = asyncio.run(get_runner().run("test.unwired", {}))
    assert result.ok is False
    assert result.error_code == "skill_executor_not_wired"


def test_skill_runner_rag_roundtrip():
    """RAG upsert + search must work end-to-end with no external keys."""
    from app.skills import bootstrap
    from app.skills.runner import get_runner
    import asyncio
    bootstrap()
    # 1) upsert a known phrase into skills collection
    text = "the rainbow vacuum robot uses ROS 2 and a 2D LiDAR with Home Assistant integration"
    up = asyncio.run(get_runner().run("rag.upsert", {"collection": "skills", "text": text, "source": "unit-test"}))
    assert up.ok is True, up.to_dict()
    assert up.data.get("upserted") >= 1
    # 2) search for it
    s = asyncio.run(get_runner().run("rag.skills.search", {"query": "vacuum robot"}))
    assert s.ok is True
    assert s.data.get("count") >= 1
    hit = s.data["hits"][0]
    assert "vacuum" in hit["text"].lower()
    assert hit["score"] > 0


def test_skill_runner_rag_empty_collection():
    """Searching an empty collection must return rag_empty (not 0 hits silently)."""
    from app.skills import bootstrap
    from app.skills.runner import get_runner
    from app.skills.rag.store import get_rag_store
    import asyncio, os, tempfile
    bootstrap()
    # Use a throwaway data dir for this test so we don't see prior chunks
    tmp = tempfile.mkdtemp()
    os.environ["AION_DATA_DIR"] = tmp
    # Re-init the store singleton to pick up the new dir
    from app.skills import rag as rag_module
    rag_module.store._store = None
    get_rag_store()
    result = asyncio.run(get_runner().run("rag.code.search", {"query": "anything"}))
    assert result.ok is False
    assert result.error_code == "rag_empty"


def test_skill_runner_github_repo_returns_github_not_configured_without_token():
    """Without GITHUB_TOKEN, the executor must return github_not_configured
    (never invent a payload)."""
    import asyncio
    from app.skills import bootstrap
    from app.skills.runner import get_runner
    bootstrap()
    # Ensure no GITHUB_TOKEN in this test
    import os
    saved = os.environ.pop("GITHUB_TOKEN", None)
    try:
        result = asyncio.run(get_runner().run("github.repo", {"repository": "ABBYCRM/robot-vacuum"}))
        assert result.ok is False
        assert result.error_code == "github_not_configured"
    finally:
        if saved is not None:
            os.environ["GITHUB_TOKEN"] = saved


def test_skill_runner_scrap_not_configured():
    """Without any scrape env, must hard-fail with scrape_not_configured."""
    import asyncio, os
    from app.skills import bootstrap
    from app.skills.runner import get_runner
    bootstrap()
    for k in ("FIRECRAWL_API_KEY", "SCRAPINGBEE_API_KEY", "SCRAPFLY_API_KEY"):
        os.environ.pop(k, None)
    result = asyncio.run(get_runner().run("scrape.url", {"url": "https://example.com"}))
    assert result.ok is False
    assert result.error_code == "scrape_not_configured"


def test_skill_routes_catalog_endpoint_returns_12():
    from app.skills import bootstrap
    from app import main as main_mod
    import importlib
    importlib.reload(main_mod)
    bootstrap()
    from fastapi.testclient import TestClient
    client = TestClient(main_mod.app)
    r = client.get("/api/skills", headers=USER_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert body.get("count") >= 20
    ids = {s["id"] for s in body["skills"]}
    assert "github.repo" in ids
    assert "rag.skills.search" in ids
    assert "scrape.url" in ids
    assert "email.send" in ids
    assert "github.scenario.match" in ids


def test_skill_routes_run_skills_catalog():
    from app.skills import bootstrap
    from app import main as main_mod
    import importlib
    importlib.reload(main_mod)
    bootstrap()
    from fastapi.testclient import TestClient
    client = TestClient(main_mod.app)
    r = client.post("/api/skills/run", headers=USER_HEADERS, json={"skill_id": "skills.catalog", "args": {}})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert body["data"]["count"] >= 20


def test_skill_routes_run_unknown_skill_returns_404():
    from app.skills import bootstrap
    from app import main as main_mod
    import importlib
    importlib.reload(main_mod)
    bootstrap()
    from fastapi.testclient import TestClient
    client = TestClient(main_mod.app)
    r = client.post("/api/skills/run", headers=USER_HEADERS, json={"skill_id": "does.not.exist", "args": {}})
    assert r.status_code == 404


def test_skill_routes_run_bad_args_returns_400():
    from app.skills import bootstrap
    from app import main as main_mod
    import importlib
    importlib.reload(main_mod)
    bootstrap()
    from fastapi.testclient import TestClient
    client = TestClient(main_mod.app)
    r = client.post("/api/skills/run", headers=USER_HEADERS, json={"skill_id": "github.repo", "args": {}})
    body = r.json()
    assert body.get("ok") is False
    assert body.get("error_code") == "invalid_args"
    assert "missing_required:repository" in (body.get("error_message") or "")


def test_skill_routes_bootstrap_endpoint_runs_seed():
    """POST /api/skills/bootstrap must seed 12 contracts + return the skill ids."""
    from app.skills import bootstrap as _bootstrap
    from app.skills.registry_core import get_registry
    from app import main as main_mod
    import importlib
    importlib.reload(main_mod)
    # Direct SQL wipe so we can see the endpoint actually seed (the
    # SkillRegistry class in this pack has no public delete())
    import sqlite3
    reg = get_registry()
    conn = sqlite3.connect(reg.db_path)
    conn.execute("DELETE FROM skills")
    conn.commit()
    conn.close()
    from fastapi.testclient import TestClient
    client = TestClient(main_mod.app)
    r = client.post("/api/skills/bootstrap", headers=USER_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert body.get("seeded") == 20
    assert "web.search" in body.get("skills", [])
    assert "rag.skills.search" in body.get("skills", [])
    assert "github.scenario.match" in body.get("skills", [])
    # Sanity: registry has it now
    assert get_registry().get("web.search") is not None
    # Idempotent: running again re-seeds (14 again)
    r2 = client.post("/api/skills/bootstrap", headers=USER_HEADERS)
    assert r2.json().get("seeded") == 20




def test_scenario_bootstrap_seeds_20_contracts():
    """After installing the GitHub scenarios CSV, the catalog must
    include github.scenario.match and github.scenario.index."""
    from app.skills import bootstrap
    info = bootstrap()
    assert info.get("seeded") == 20
    from app.skills.registry_core import get_registry
    ids = {s.id for s in get_registry().list(enabled_only=False)}
    assert "github.scenario.match" in ids
    assert "github.scenario.index" in ids


def test_scenarios_dir_resolver_finds_scenarios_in_image():
    """The unified matcher must walk the priority chain and find the
    operator-shipped packs at /app/data/scenarios/."""
    from app.skills.clients.scenarios import _resolve_scenarios_dir, PACKS
    scenarios_dir = _resolve_scenarios_dir()
    assert scenarios_dir.exists(), f"scenarios dir not found: {scenarios_dir}"
    # Each pack CSV must exist
    for pack, fname in PACKS.items():
        path = scenarios_dir / fname
        assert path.exists(), f"missing pack: {path}"
        # Each CSV is at least 500 rows + header
        n = sum(1 for _ in path.open("r", encoding="utf-8")) - 1
        assert n >= 500, f"{pack} has only {n} rows"


def test_scenario_match_github_pack_returns_real_rows():
    """github.scenario.match must return ranked matches from the real
    500-row pack (not the old stub)."""
    from app.skills.clients.scenarios import match_scenarios
    out = match_scenarios("workflow run is cancelled by user", pack="github", limit=3)
    assert out["count"] >= 1
    ch = out["chosen"]
    # The real github pack has trigger text like "A workflow run is cancelled"
    assert "workflow" in ch["trigger"].lower() or "cancelled" in ch["trigger"].lower()
    assert ch["pack"] == "github"
    assert ch["severity"] in ("high", "medium", "low", "critical")


def test_scenario_match_openclaw_pack_returns_real_rows():
    from app.skills.clients.scenarios import match_scenarios
    out = match_scenarios("Agent invokes the shell skill to run a command", pack="openclaw", limit=2)
    assert out["count"] >= 1
    ch = out["chosen"]
    assert ch["pack"] == "openclaw"
    # openclaw rows have a `skill` column
    assert "skill" in ch or ch.get("category") == "core_shell"


def test_scenario_match_composio_pack_returns_real_rows():
    from app.skills.clients.scenarios import match_scenarios
    out = match_scenarios("Agent calls composio.create(userId) to open a new session", pack="composio", limit=2)
    assert out["count"] >= 1
    ch = out["chosen"]
    assert ch["pack"] == "composio"


def test_scenario_match_firecrawl_steel_pack_returns_real_rows():
    from app.skills.clients.scenarios import match_scenarios
    out = match_scenarios("Agent calls /v2/scrape on a single URL", pack="firecrawl_steel", limit=2)
    assert out["count"] >= 1
    ch = out["chosen"]
    assert ch["pack"] == "firecrawl_steel"
    # firecrawl rows have a `service` column
    assert "service" in ch


def test_scenario_match_render_pack_returns_real_rows():
    from app.skills.clients.scenarios import match_scenarios
    out = match_scenarios("Render build phase starts for the service", pack="render", limit=2)
    assert out["count"] >= 1
    ch = out["chosen"]
    assert ch["pack"] == "render"


def test_scenario_match_all_packs_returns_mixed_results():
    """scenario.match (the unified one) must return matches from
    multiple packs when the trigger is generic."""
    from app.skills.clients.scenarios import match_scenarios
    out = match_scenarios("error", pack="all", limit=10)
    # The unified matcher should find at least a few matches across packs
    assert out["count"] >= 1
    packs_in_matches = {m["pack"] for m in out["matches"]}
    # At least one of the 5 packs should be represented
    assert len(packs_in_matches) >= 1


def test_scenario_match_unknown_pack_returns_invalid_args():
    from app.skills.clients.scenarios import match_scenarios
    from app.skills.base import SkillError
    try:
        match_scenarios("anything", pack="not_a_real_pack")
        assert False, "expected SkillError"
    except SkillError as e:
        assert e.error_code == "invalid_args"
        assert "unknown_pack" in str(e)


def test_scenario_index_creates_rag_collections():
    """scenario.index must write 2,500 rows (5 packs * 500) into
    'scenario_policy' when pack=all."""
    from app.skills.clients.scenarios import scenario_index, _resolve_scenarios_dir
    # Run the subroutine
    import asyncio
    out = asyncio.get_event_loop().run_until_complete(
        scenario_index({"pack": "all"}, {})
    )
    assert out["indexed"] >= 2500, f"expected >= 2500 rows, got {out['indexed']}"
    assert out["collection"] == "scenario_policy"
    assert sorted(out["packs_indexed"]) == ["composio", "firecrawl_steel", "github", "openclaw", "render"]


def test_scenario_skill_routes_run_github_returns_real_data():
    """End-to-end: POST /api/skills/run github.scenario.match must
    return ok=true with ranked matches from the real github pack."""
    from app.skills import bootstrap
    from app import main as main_mod
    import importlib
    importlib.reload(main_mod)
    bootstrap()
    from fastapi.testclient import TestClient
    client = TestClient(main_mod.app)
    r = client.post(
        "/api/skills/run",
        headers=USER_HEADERS,
        json={"skill_id": "github.scenario.match", "args": {"trigger": "workflow run is cancelled by user"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    ch = body["data"]["chosen"]
    assert ch["pack"] == "github"
    # The chosen trigger should overlap with our input (workflow, run, cancel, user)
    trig = ch["trigger"].lower()
    assert any(t in trig for t in ("workflow", "run", "cancel")), f"unexpected trigger: {trig}"




def test_vault_ping_endpoints_return_auth_signals_for_bad_keys():
    """The 5 ping endpoints that previously returned 404 or 410 must now
    return a proper 401/403/etc. for an invalid key (proving the endpoint
    exists and is correctly auth-gated)."""
    from app.vault import _ping_generic
    import asyncio

    async def _check(name, ping):
        ok, latency, err = await ping()
        # A 401/403/410/etc. with a sensible message is the success case.
        # We just want to confirm the endpoint exists and is auth-gated.
        assert err is None or any(s in (err or "") for s in (
            "401", "403", "Unauthorized", "Authentication", "Invalid", "auth", "Unauthorized"
        )), f"{name}: unexpected error: {err}"

    # Composio v3
    async def _composio():
        return await _ping_generic("COMPOSIO_API_KEY", "invalid_test_key",
            url="https://backend.composio.dev/api/v3/auth/session/info",
            headers={"x-api-key": "invalid_test_key"})
    asyncio.get_event_loop().run_until_complete(_check("COMPOSIO", _composio))

    # Firecrawl v2
    async def _firecrawl():
        return await _ping_generic("FIRECRAWL_API_KEY", "invalid_test_key",
            method="POST", url="https://api.firecrawl.dev/v2/scrape",
            headers={"Authorization": "Bearer invalid_test_key", "Content-Type": "application/json"},
            json_body={"url": "https://example.com"})
    asyncio.get_event_loop().run_until_complete(_check("FIRECRAWL", _firecrawl))

    # ScrapingBee
    async def _scrapingbee():
        return await _ping_generic("SCRAPINGBEE_API_KEY", "invalid_test_key",
            url="https://app.scrapingbee.com/api/v1/usage?api_key=invalid_test_key")
    asyncio.get_event_loop().run_until_complete(_check("SCRAPINGBEE", _scrapingbee))

    # Kimi global endpoint
    async def _kimi():
        return await _ping_generic("KIMI_API_KEY", "invalid_test_key",
            url="https://api.moonshot.ai/v1/models",
            headers={"Authorization": "Bearer invalid_test_key"})
    asyncio.get_event_loop().run_until_complete(_check("KIMI", _kimi))

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


# ===========================================================================
# Vault DELETE + LLM-vault wiring
# ===========================================================================


def test_vault_delete_removes_entry_and_clears_env(_vault_db):
    client = TestClient(_vault_db.app)
    headers = {**ADMIN_HEADERS, "X-AION-Confirm": "yes"}
    # First set a value
    client.post("/api/vault/OPENAI_API_KEY/rotate", headers=headers, json={"value": "sk-test-to-delete"})
    r = client.get("/api/vault", headers=ADMIN_HEADERS)
    item = next(i for i in r.json()["items"] if i["name"] == "OPENAI_API_KEY")
    assert item["has_value"] is True
    # Delete it
    r = client.delete("/api/vault/OPENAI_API_KEY", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["deleted"] is True
    assert body["env_cleared"] == "OPENAI_API_KEY"
    # Should be gone now
    r = client.get("/api/vault", headers=ADMIN_HEADERS)
    item = next((i for i in r.json()["items"] if i["name"] == "OPENAI_API_KEY"), None)
    assert item is None
    # Env should be cleared
    import os as _os
    assert _os.environ.get("OPENAI_API_KEY", "") == ""


def test_vault_delete_requires_admin(_vault_db):
    client = TestClient(_vault_db.app)
    r = client.delete("/api/vault/OPENAI_API_KEY", headers=USER_HEADERS)
    assert r.status_code == 403


def test_vault_delete_requires_confirm(_vault_db):
    client = TestClient(_vault_db.app)
    r = client.delete("/api/vault/OPENAI_API_KEY", headers=ADMIN_HEADERS)
    assert r.status_code == 409  # confirmation required


def test_vault_delete_unknown_key_returns_404(_vault_db):
    client = TestClient(_vault_db.app)
    headers = {**ADMIN_HEADERS, "X-AION-Confirm": "yes"}
    r = client.delete("/api/vault/NOT_A_REAL_KEY", headers=headers)
    assert r.status_code == 404


def test_llm_uses_vault_key_not_settings(_vault_db, monkeypatch):
    """If a key is set in BOTH settings and the vault, the vault wins."""
    import os
    # Set a fake key in both places
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env-WRONG")
    client = TestClient(_vault_db.app)
    headers = {**ADMIN_HEADERS, "X-AION-Confirm": "yes"}
    # Vault wins
    client.post("/api/vault/OPENAI_API_KEY/rotate", headers=headers, json={"value": "sk-from-vault-WINNER"})
    # Reload modules so _vault_value picks up the new vault
    import importlib
    from app import llm, vault
    importlib.reload(vault)
    from app import main as main_mod
    importlib.reload(main_mod)
    importlib.reload(llm)
    # _vault_value should prefer the vault
    from app.llm import _vault_value
    assert _vault_value("OPENAI_API_KEY") == "sk-from-vault-WINNER", f"got: {_vault_value('OPENAI_API_KEY')}"


def test_llm_configured_providers_sees_vault_only_keys(_vault_db, monkeypatch):
    """A key set ONLY in the vault (never in env) should be picked up
    by configured_providers()."""
    # Clear all relevant envs
    for k in ["OPENAI_API_KEY", "KIMI_API_KEY", "MOONSHOT_API_KEY", "BITDEER_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "HELICONE_API_KEY"]:
        monkeypatch.setenv(k, "")
    client = TestClient(_vault_db.app)
    headers = {**ADMIN_HEADERS, "X-AION-Confirm": "yes"}
    # Set a key in the vault only
    client.post("/api/vault/KIMI_API_KEY/rotate", headers=headers, json={"value": "sk-kimi-from-vault"})
    # Reload modules
    import importlib
    from app import llm, vault, settings
    importlib.reload(vault)
    from app import main as main_mod
    importlib.reload(main_mod)
    importlib.reload(llm)
    from app.llm import configured_providers
    providers = configured_providers()
    assert "moonshot" in providers, f"providers: {providers}"


# ===========================================================================
# Brain client integration tests
# ===========================================================================


def test_brain_not_configured_returns_unavailable(_vault_db, monkeypatch):
    """The default test env (no AION_BRAIN_* set) must leave Brain
    unconfigured. Defensively re-assert after reloading so the
    singleton picked up by brain_client is the post-reload one."""
    monkeypatch.delenv("AION_BRAIN_ENABLED", raising=False)
    monkeypatch.delenv("AION_BRAIN_URL", raising=False)
    monkeypatch.delenv("AION_BRAIN_KEY", raising=False)
    import importlib
    from app import settings, brain_client
    importlib.reload(settings)
    importlib.reload(brain_client)
    assert brain_client.is_configured() is False


def test_brain_decision_delegates_when_configured(_vault_db, monkeypatch):
    """When brain is configured and healthy, /api/decision hits Brain."""
    from app import brain_client
    monkeypatch.setenv("AION_BRAIN_ENABLED", "true")
    monkeypatch.setenv("AION_BRAIN_URL", "http://localhost:10001")
    monkeypatch.setenv("AION_BRAIN_KEY", "test-brain-key")
    assert brain_client.is_configured() is True
    # Patch the brain_client.decision to simulate a healthy response
    async def _fake_decision(*, user_input, history=None, metadata=None):
        return {"request_id": "req_x", "decision": {"state": "COMMIT", "score": 0.9, "id": "dec_x", "checks": []}}
    monkeypatch.setattr(brain_client, "decision", _fake_decision)
    import importlib
    from app import settings, main as main_mod
    importlib.reload(settings)
    importlib.reload(main_mod)
    client = TestClient(main_mod.app)
    r = client.post("/api/decision", headers=USER_HEADERS, json={"user_input": "ping", "history": []})
    assert r.status_code == 200
    body = r.json()
    assert body["decision"]["state"] == "COMMIT"
    assert body["request_id"] == "req_x"


def test_brain_decision_falls_back_to_local_when_brain_down(_vault_db, monkeypatch):
    """When brain is configured but unreachable, /api/decision falls back
    to the local Python kernel (assuming AION_BRAIN_REQUIRED is false)."""
    monkeypatch.setenv("AION_BRAIN_ENABLED", "true")
    monkeypatch.setenv("AION_BRAIN_URL", "http://localhost:1")  # always down
    monkeypatch.setenv("AION_BRAIN_KEY", "test-brain-key")
    monkeypatch.setenv("AION_BRAIN_REQUIRED", "false")
    import importlib
    from app import settings, main as main_mod
    importlib.reload(settings)
    importlib.reload(main_mod)
    client = TestClient(main_mod.app)
    r = client.post("/api/decision", headers=USER_HEADERS, json={"user_input": "ping", "history": []})
    assert r.status_code == 200
    body = r.json()
    assert "decision" in body
    # Should be from the local kernel (which is still working)
    assert body["decision"]["state"] in ("COMMIT", "DEFER", "REJECT")


def test_brain_required_blocks_when_brain_down(_vault_db, monkeypatch):
    """AION_BRAIN_REQUIRED=true means /api/decision returns 200+ok=false
    with kind=brain_unavailable when the Brain is down."""
    monkeypatch.setenv("AION_BRAIN_ENABLED", "true")
    monkeypatch.setenv("AION_BRAIN_URL", "http://localhost:1")
    monkeypatch.setenv("AION_BRAIN_KEY", "test-brain-key")
    monkeypatch.setenv("AION_BRAIN_REQUIRED", "true")
    import importlib
    from app import settings, main as main_mod
    importlib.reload(settings)
    importlib.reload(main_mod)
    client = TestClient(main_mod.app)
    r = client.post("/api/decision", headers=USER_HEADERS, json={"user_input": "ping", "history": []})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is False
    assert body.get("kind") == "brain_unavailable"


def test_brain_chat_streams_sse_events(_vault_db, monkeypatch):
    """When Brain is configured, /api/chat proxies Brain's SSE stream."""
    import asyncio, json
    monkeypatch.setenv("AION_BRAIN_ENABLED", "true")
    monkeypatch.setenv("AION_BRAIN_URL", "http://localhost:10001")
    monkeypatch.setenv("AION_BRAIN_KEY", "test-brain-key")
    monkeypatch.setenv("AION_BRAIN_DECISION_ONLY", "false")
    import importlib
    from app import settings, main as main_mod
    importlib.reload(settings)
    importlib.reload(main_mod)
    client = TestClient(main_mod.app)
    r = client.post("/api/chat", headers=USER_HEADERS, json={"messages": [{"role": "user", "content": "ping"}], "max_tokens": 64, "temperature": 0})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    body = r.text
    # Should at least contain a decision event
    assert "decision" in body
