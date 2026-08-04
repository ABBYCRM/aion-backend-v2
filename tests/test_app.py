from __future__ import annotations

from fastapi.testclient import TestClient
from app.main import app

USER_HEADERS = {"X-AION-Key": "test-user-key"}
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
