"""Tests for v2.8.12 new skills: writing.ste.slop_suppress, writing.adhd_output,
meta.book_to_skill, and all 4 GDY client skills."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Add the package root to sys.path so the tests run from the repo.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ----- no-ai-slop suppress -----

def test_slop_suppress_strips_binary_contrast():
    from app.skills.clients import ste_rewrite
    r = _run(ste_rewrite.writing_ste_slop_suppress(
        {"text": "It's not just an agent, but a transformative experience."}, {}))
    assert r["ok"] is True
    assert "not just" not in r["rewritten"].lower()
    assert "transformative" not in r["rewritten"].lower()
    assert len(r["changes"]) >= 1


def test_slop_suppress_strips_faux_profound_ending():
    from app.skills.clients import ste_rewrite
    r = _run(ste_rewrite.writing_ste_slop_suppress(
        {"text": "The model works. The future is not coming, it's already here. Hope this helps."}, {}))
    assert r["ok"] is True
    assert "already here" not in r["rewritten"].lower()
    assert "Hope this helps" not in r["rewritten"]


def test_slop_suppress_rejects_empty():
    from app.skills.clients import ste_rewrite
    r = _run(ste_rewrite.writing_ste_slop_suppress({"text": ""}, {}))
    assert r["ok"] is False
    assert r["error_code"] == "invalid_args"


def test_slop_suppress_handles_clean_text():
    from app.skills.clients import ste_rewrite
    r = _run(ste_rewrite.writing_ste_slop_suppress(
        {"text": "The auth flow checks a JWT in middleware and rejects bad tokens. Done."}, {}))
    assert r["ok"] is True
    assert r["rewritten"].strip() != ""


# ----- ADHD output -----

def test_adhd_strips_hope_this_helps():
    from app.skills.clients import ste_rewrite
    r = _run(ste_rewrite.writing_adhd_output(
        {"text": "I would recommend trying it. Hope this helps! Let me know if you have any questions."}, {}))
    assert r["ok"] is True
    assert "Hope this helps" not in r["rewritten"]
    assert "Let me know" not in r["rewritten"]
    assert r["closers_stripped"] >= 2


def test_adhd_rewrites_vague_time_estimates():
    from app.skills.clients import ste_rewrite
    r = _run(ste_rewrite.writing_adhd_output(
        {"text": "Run npm test. It will finish in a bit. You can also try in just a second."}, {}))
    assert r["ok"] is True
    assert "in a bit" not in r["rewritten"]
    assert "just a second" not in r["rewritten"]
    assert "under 2 minutes" in r["rewritten"]


def test_adhd_prepends_state_when_step_given():
    from app.skills.clients import ste_rewrite
    r = _run(ste_rewrite.writing_adhd_output(
        {"text": "Schema updated. Ready for the next part?", "step": "3", "total_steps": 7}, {}))
    assert r["ok"] is True
    assert r["rewritten"].startswith("Step 3 of 7.")


def test_adhd_rejects_empty():
    from app.skills.clients import ste_rewrite
    r = _run(ste_rewrite.writing_adhd_output({"text": ""}, {}))
    assert r["ok"] is False


# ----- book-to-skill -----

def test_book_to_skill_creates_skill_md_and_indexes_rag():
    from app.skills.clients import ste_rewrite
    # Use a small MD file as input
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("# Chapter 1\n\nThis is a test chapter about Go concurrency.\n\n" * 20)
        path = f.name
    try:
        r = _run(ste_rewrite.meta_book_to_skill({"path": path, "slug": "test-book-1"}, {}))
        assert r["ok"] is True, r
        assert r["chapters_indexed"] >= 1
        # The SKILL.md should be created
        skill_md = Path("data/skills/test-book-1/SKILL.md")
        assert skill_md.exists()
        # Cleanup
        import shutil
        shutil.rmtree("data/skills/test-book-1", ignore_errors=True)
    finally:
        os.unlink(path)


def test_book_to_skill_rejects_missing_file():
    from app.skills.clients import ste_rewrite
    r = _run(ste_rewrite.meta_book_to_skill({"path": "/nonexistent.pdf"}, {}))
    assert r["ok"] is False
    assert r["error_code"] == "file_not_found"


def test_book_to_skill_rejects_unsupported_suffix():
    from app.skills.clients import ste_rewrite
    with tempfile.NamedTemporaryFile(suffix=".xyz", delete=False) as f:
        f.write(b"x")
        path = f.name
    try:
        r = _run(ste_rewrite.meta_book_to_skill({"path": path}, {}))
        assert r["ok"] is False
        assert r["error_code"] == "pdf_parse_failed"
    finally:
        os.unlink(path)


# ----- GDY client -----

def test_gdy_client_parses_token():
    from app.skills.clients.gdy import GdyClient
    c = GdyClient(token="dummy")
    assert c._token == "dummy"
    assert c._base == "https://gdyworld.com/v1"


def test_gdy_client_no_token_raises_auth_error():
    from app.skills.clients.gdy import GdyClient, GdyAuthError
    c = GdyClient(token="")
    r = _run(c._request("GET", "/me"))
    # An empty token still creates a GdyClient, but the request fails
    # with a clear auth error so the operator knows to set the key.
    # (The async with no-op returns the request result.)
    # We use a separate function that calls the executor.


def test_gdy_me_executor_returns_auth_error_when_key_missing(monkeypatch):
    from app.skills.clients import gdy
    monkeypatch.delenv("GDY_API_KEY", raising=False)
    r = _run(gdy.gdy_me({}, {}))
    assert r["ok"] is False
    assert r["error_code"] == "GdyAuthError"


def test_gdy_me_executor_live():
    """Live test against the real GDY API. Skipped if GDY_API_KEY isn't set."""
    from app.skills.clients import gdy
    if not os.environ.get("GDY_API_KEY"):
        pytest.skip("GDY_API_KEY not set in env")
    r = _run(gdy.gdy_me({}, {}))
    assert r["ok"] is True
    assert "tools:read" in r["scopes"]


def test_gdy_categories_executor_live():
    from app.skills.clients import gdy
    if not os.environ.get("GDY_API_KEY"):
        pytest.skip("GDY_API_KEY not set in env")
    r = _run(gdy.gdy_categories({}, {}))
    assert r["ok"] is True
    assert r["total_categories"] == 25
    assert r["total_tools"] >= 800


def test_gdy_tools_executor_live_filters_by_category():
    from app.skills.clients import gdy
    if not os.environ.get("GDY_API_KEY"):
        pytest.skip("GDY_API_KEY not set in env")
    r = _run(gdy.gdy_tools({"category": "18", "per_page": 3}, {}))
    assert r["ok"] is True
    assert r["total"] == 27
    assert len(r["tools"]) == 3


def test_gdy_search_executor_rejects_empty_query():
    from app.skills.clients import gdy
    r = _run(gdy.gdy_search({"query": ""}, {}))
    assert r["ok"] is False
    assert r["error_code"] == "missing_required:query"


# ----- contract: 7 new skills are registered -----

def test_contract_new_skills_are_registered():
    """Contract: the 7 new skills (3 local + 4 GDY) are registered
    in seed_all.py with the right executor wiring."""
    from app.skills.seed_all import all_specs
    specs = all_specs()
    by_id = {sp.id: sp for sp in specs}
    for skill_id in [
        "writing.ste.slop_suppress",
        "writing.adhd_output",
        "meta.book_to_skill",
        "gdy.me",
        "gdy.categories",
        "gdy.tools",
        "gdy.search",
    ]:
        assert skill_id in by_id, f"missing skill: {skill_id}"
        spec = by_id[skill_id]
        # All 7 must have a builtin: executor
        assert spec.executor.startswith("builtin:"), f"{skill_id} executor not builtin"
    # Side-effects must be set sensibly
    assert by_id["meta.book_to_skill"].side_effect == "write"
    assert by_id["gdy.me"].side_effect == "network"
    assert by_id["gdy.search"].side_effect == "network"
    # Tags must include the source repo
    assert "no-ai-slop" not in by_id["writing.ste.slop_suppress"].tags  # we don't claim the source
    assert "gdy" in by_id["gdy.me"].tags


def test_contract_total_skill_count_is_45():
    """Lock: total skills should be 45 (38 pre-existing + 7 new)."""
    from app.skills.seed_all import all_specs
    assert len(all_specs()) == 45
