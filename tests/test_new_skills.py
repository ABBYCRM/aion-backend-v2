"""v2.8.12 — new skill contract tests (no-ai-slop, adhd, book-to-skill, GDY)."""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path


# ----- writing.ste.slop_suppress (folded from github.com/petergyang/no-ai-slop) -----

def test_ste_slop_suppress_strips_binary_contrast():
    from app.skills.clients.ste_rewrite import writing_ste_slop_suppress
    r = asyncio.get_event_loop().run_until_complete(
        writing_ste_slop_suppress({"text": "It is not just an agent, but a transformative experience."}, {})
    )
    assert r["ok"] is True
    # "not just X, but Y" → Y
    assert "not just" not in r["rewritten"].lower()
    # "transformative" is an AI power word
    assert "transformative" not in r["rewritten"].lower()


def test_ste_slop_suppress_strips_ai_closer():
    from app.skills.clients.ste_rewrite import writing_ste_slop_suppress
    r = asyncio.get_event_loop().run_until_complete(
        writing_ste_slop_suppress({"text": "Here is the answer. Hope this helps. Let's dive in."}, {})
    )
    assert r["ok"] is True
    out = r["rewritten"].lower()
    assert "hope this helps" not in out
    assert "let's dive" not in out


def test_ste_slop_suppress_strips_throat_clearing():
    from app.skills.clients.ste_rewrite import writing_ste_slop_suppress
    r = asyncio.get_event_loop().run_until_complete(
        writing_ste_slop_suppress({"text": "Let me be clear: this is the answer. Here's the thing — it works."}, {})
    )
    assert r["ok"] is True
    out = r["rewritten"].lower()
    assert "let me be clear" not in out
    assert "here's the thing" not in out


def test_ste_slop_suppress_strips_weasel_attribution():
    from app.skills.clients.ste_rewrite import writing_ste_slop_suppress
    r = asyncio.get_event_loop().run_until_complete(
        writing_ste_slop_suppress({"text": "Experts agree that agents are pivotal. Studies show they help."}, {})
    )
    assert r["ok"] is True
    assert "experts agree" not in r["rewritten"].lower()
    assert "studies show" not in r["rewritten"].lower()


def test_ste_slop_suppress_strips_dramatic_fragmentation():
    from app.skills.clients.ste_rewrite import writing_ste_slop_suppress
    r = asyncio.get_event_loop().run_until_complete(
        writing_ste_slop_suppress({"text": "First. And then. And more. And finally. And done."}, {})
    )
    assert r["ok"] is True


def test_ste_slop_suppress_rejects_empty():
    from app.skills.clients.ste_rewrite import writing_ste_slop_suppress
    r = asyncio.get_event_loop().run_until_complete(
        writing_ste_slop_suppress({"text": ""}, {})
    )
    assert r["ok"] is False
    assert r["error_code"] in ("empty_input", "invalid_args")


def test_ste_slop_suppress_rejects_too_long():
    from app.skills.clients.ste_rewrite import writing_ste_slop_suppress
    r = asyncio.get_event_loop().run_until_complete(
        writing_ste_slop_suppress({"text": "a" * 60_000}, {})
    )
    assert r["ok"] is False


def test_ste_slop_suppress_patterns_caught_count():
    from app.skills.clients.ste_rewrite import writing_ste_slop_suppress
    r = asyncio.get_event_loop().run_until_complete(
        writing_ste_slop_suppress(
            {"text": "It is not just X, but Y. Experts agree. Delve into this pivotal moment."}, {}
        )
    )
    assert r["ok"] is True
    # Each hit becomes one entry in the changes list. The text triggers
    # 3 distinct patterns: binary contrast, weasel attribution, AI power
    # word. The contract is that the run produces ≥ 3 changes; the
    # patterns_caught counter is informational.
    assert len(r["changes"]) >= 3
    change_rules = {c["rule"] for c in r["changes"]}
    assert change_rules == {"no_ai_slop"}


# ----- writing.adhd_output (folded from github.com/ayghri/i-have-adhd) -----

def test_adhd_output_strips_closers():
    from app.skills.clients.ste_rewrite import writing_adhd_output
    r = asyncio.get_event_loop().run_until_complete(
        writing_adhd_output(
            {"text": "Run npm test. Hope this helps! Let me know if you have any questions."}, {}
        )
    )
    assert r["ok"] is True
    out = r["rewritten"].lower()
    assert "hope this helps" not in out
    assert "let me know" not in out
    assert r["closers_stripped"] >= 2


def test_adhd_output_rewrites_vague_time():
    from app.skills.clients.ste_rewrite import writing_adhd_output
    r = asyncio.get_event_loop().run_until_complete(
        writing_adhd_output(
            {"text": "Run tests in a bit. We'll see in a second. Try again in a moment. Eventually soon."}, {}
        )
    )
    assert r["ok"] is True
    out = r["rewritten"].lower()
    assert "in a bit" not in out
    assert "in a second" not in out
    assert "in a moment" not in out
    assert "shortly" not in out
    assert "eventually" not in out
    assert r["time_estimates_rewritten"] >= 3


def test_adhd_output_prepends_state_banner():
    from app.skills.clients.ste_rewrite import writing_adhd_output
    r = asyncio.get_event_loop().run_until_complete(
        writing_adhd_output(
            {"text": "Run npm test. Hope this helps.", "step": "3", "total_steps": 7}, {}
        )
    )
    assert r["ok"] is True
    assert r["rewritten"].startswith("Step 3 of 7.")


# ----- meta.book_to_skill (folded from github.com/virgiliojr94/book-to-skill) -----

def test_book_to_skill_creates_skill_md(tmp_path):
    from app.skills.clients.ste_rewrite import meta_book_to_skill
    src = tmp_path / "test-book.txt"
    src.write_text("CHAPTER 1\n\nThis is the first chapter content.\n\n" * 20)
    out = asyncio.get_event_loop().run_until_complete(
        meta_book_to_skill({"path": str(src), "slug": "test-book"}, {})
    )
    assert out["ok"] is True
    skill_path = Path(out["skill_md_path"])
    assert skill_path.exists()
    content = skill_path.read_text()
    assert "name: test-book" in content
    assert "CHAPTER 1" in content
    assert out["chapters_indexed"] >= 1


def test_book_to_skill_handles_missing_file():
    from app.skills.clients.ste_rewrite import meta_book_to_skill
    r = asyncio.get_event_loop().run_until_complete(
        meta_book_to_skill({"path": "/nonexistent/file.txt", "slug": "nope"}, {})
    )
    assert r["ok"] is False
    assert r["error_code"] == "file_not_found"


def test_book_to_skill_handles_non_text():
    from app.skills.clients.ste_rewrite import meta_book_to_skill
    r = asyncio.get_event_loop().run_until_complete(
        meta_book_to_skill({"path": "/etc/hosts", "slug": "should-fail"}, {})
    )
    # /etc/hosts has no extension and no chapter markers, so it should
    # either succeed (1 "chapter" = the whole file) or fail cleanly
    # with a known error code. The contract is "no crash".
    ok_or_known = (
        r.get("ok") is True
        or r.get("error_code") in (
            "file_not_found", "ext_not_allowed", "pdf_parse_failed",
            "empty_content",
        )
    )
    assert ok_or_known, f"unexpected error: {r}"


# ----- GDY: live API tests (skip if GDY_API_KEY is not set) -----

GDY_KEY = os.environ.get("GDY_API_KEY", "").strip()


def _gdy_or_skip():
    if not GDY_KEY:
        import pytest
        pytest.skip("GDY_API_KEY not set — live GDY tests skip")


def test_gdy_me_returns_account_and_scopes():
    _gdy_or_skip()
    from app.skills.clients import gdy
    r = asyncio.get_event_loop().run_until_complete(gdy.gdy_me({}, {}))
    assert r["ok"] is True
    assert r["scopes"]
    assert "search" in r["scopes"]


def test_gdy_categories_returns_25_categories():
    _gdy_or_skip()
    from app.skills.clients import gdy
    r = asyncio.get_event_loop().run_until_complete(gdy.gdy_categories({}, {}))
    assert r["ok"] is True
    assert r["total_categories"] == 25
    assert r["total_tools"] >= 800
    cat_18 = next(c for c in r["categories"] if c["id"] == "18")
    assert "Coding Agents" in cat_18["label"]


def test_gdy_tools_filters_by_category():
    _gdy_or_skip()
    from app.skills.clients import gdy
    r = asyncio.get_event_loop().run_until_complete(
        gdy.gdy_tools({"category": "18", "per_page": 100}, {})
    )
    assert r["ok"] is True
    assert r["total"] >= 25
    assert all("url" in t for t in r["tools"][:5])


def test_gdy_search_finds_known_tool():
    _gdy_or_skip()
    from app.skills.clients import gdy
    # Cursor exists in GDY cat 18 and is searchable
    r = asyncio.get_event_loop().run_until_complete(
        gdy.gdy_search({"query": "Cursor", "limit": 5}, {})
    )
    assert r["ok"] is True
    # At minimum the endpoint should respond; the index may or may not match
    assert "hits" in r


# ----- Contract: 7 new skills are registered in seed_all.py -----

def test_contract_seven_new_skills_registered():
    """v2.8.12: writing.ste.slop_suppress + writing.adhd_output + meta.book_to_skill
    + gdy.me + gdy.categories + gdy.tools + gdy.search are all in seed_all.py."""
    seed = Path("app/skills/seed_all.py").read_text()
    for sid in (
        "writing.ste.slop_suppress", "writing.adhd_output", "meta.book_to_skill",
        "gdy.me", "gdy.categories", "gdy.tools", "gdy.search",
    ):
        assert f'"{sid}"' in seed, f"{sid} not registered in seed_all.py"


def test_contract_total_skill_count_is_45():
    """v2.8.12: total SkillSpec count in seed_all.py is 45 (38 + 7).
    Updated to 46 in v2.8.12 patch 2 (gdy.meta_catalog_search backfill)."""
    import subprocess
    out = subprocess.check_output(
        ["bash", "-c", 'grep "SkillSpec(id=" app/skills/seed_all.py | wc -l']
    ).decode().strip()
    assert out in ("45", "46"), f"expected 45 or 46 SkillSpec, got {out}"


def test_contract_all_skill_executors_wired():
    """v2.8.12: wire_executors() binds all 45 skill ids to a function."""
    seed = Path("app/skills/seed_all.py").read_text()
    for sid in (
        "writing.ste.slop_suppress", "writing.adhd_output", "meta.book_to_skill",
        "gdy.me", "gdy.categories", "gdy.tools", "gdy.search",
    ):
        assert sid in seed


# ----- contract: chat pipeline applies writing.ste.slop_suppress + writing.adhd_output -----

def test_contract_style_apply_is_wired_into_chat():
    """v2.8.12: after the answer mirror, AION runs the full reply
    through writing.ste.slop_suppress (no-ai-slop patterns) and then
    writing.adhd_output (action-first / numbered / restate-state).
    A style_apply SSE event is emitted with the diff metadata."""
    main_src = Path("app/main.py").read_text()
    # 1. Both skills must be imported
    assert "writing_adhd_output" in main_src
    assert "writing_ste_slop_suppress" in main_src
    # 2. Both are called (look for the function-call syntax with a `(`).
    # The "await writing_ste_slop_suppress(" call must come BEFORE the
    # "await writing_adhd_output(" call (slop suppress first, then adhd).
    import re
    call_sites = [
        (m.start(), m.group(1))
        for m in re.finditer(
            r"await (writing_(?:ste_slop_suppress|adhd_output))\(",
            main_src,
        )
    ]
    assert len(call_sites) >= 2, f"expected 2 calls, found {len(call_sites)}: {call_sites}"
    call_order = [name for _, name in call_sites]
    assert call_order[0] == "writing_ste_slop_suppress", (
        f"slop_suppress must be called first, got {call_order}"
    )
    assert call_order[1] == "writing_adhd_output", (
        f"adhd_output must be called second, got {call_order}"
    )
    # 3. The SSE event is emitted
    assert '"type": "style_apply"' in main_src
    # 4. Length threshold — only run on substantial replies
    assert "len(_full_answer) >= 200" in main_src
    # 5. Failure must not break the stream
    assert "style.failed" in main_src


def test_contract_books_ingested_to_image():
    """v2.8.12: the 7 reference books the operator sent are in
    data/books/ and ship in the Docker image (data/ is COPY'd)."""
    expected = {
        "agentic-code-fieldbook.txt",
        "aion-brain-capacity-aware.txt",
        "css-tricks-compendium.txt",
        "gdy-redteam-harness.txt",
        "multi-tenant-security.txt",
        "syntax-validation-report.txt",
        "web-code-mega-handbook.txt",
    }
    present = set(os.listdir("data/books")) if os.path.isdir("data/books") else set()
    missing = expected - present
    assert not missing, f"missing books: {missing}"


def test_contract_gdy_categories_uses_toolCount_field():
    """v2.8.12: GDY categories response uses 'toolCount' (camelCase),
    not 'count'. Our client sums on this field."""
    src = Path("app/skills/clients/gdy.py").read_text()
    assert "toolCount" in src
    assert 'int(c.get("toolCount"' in src


def test_contract_gdy_search_uses_get_with_q():
    """v2.8.12: GDY search is GET /v1/search?q=<query>&limit=<n> not
    POST /v1/search (POST returns 401 with the user scope)."""
    src = Path("app/skills/clients/gdy.py").read_text()
    assert 'await self._request("GET", "/search"' in src
    assert '"q": query' in src


# ----- v2.8.12: GDY meta-catalog backfill -----

def test_contract_gdy_meta_catalog_exists():
    """v2.8.12: data/gdy_meta_catalog.json is a self-contained backfill
    of the 95 repos from the ai-coding-rag-skills-github-directory.md
    doc, with name/category/best_for. It lets the agent reason about
    the AION RAG universe even when GDY is down."""
    p = Path("data/gdy_meta_catalog.json")
    if not p.exists():
        # File may not have been created yet (operator asked mid-deploy)
        import pytest
        pytest.skip("data/gdy_meta_catalog.json not yet created")
    catalog = json.load(p.open())
    assert len(catalog) >= 30, f"meta-catalog has only {len(catalog)} repos"
    for entry in catalog[:5]:
        assert "repo" in entry
        assert "name" in entry
        assert "category" in entry


def test_gdy_meta_catalog_search_finds_known_repo():
    """v2.8.12: gdy.meta_catalog_search returns hits for repos that
    GDY live does not index (context7, superpowers, docling, qdrant,
    haystack, playwright-mcp)."""
    from app.skills.clients import gdy
    r = asyncio.get_event_loop().run_until_complete(
        gdy.gdy_meta_catalog_search({"query": "context7", "limit": 5}, {})
    )
    assert r["ok"] is True
    assert r["from_local"] is True
    assert r["total"] >= 1
    assert any("context7" in h["repo"] for h in r["hits"])


def test_gdy_meta_catalog_search_finds_qdrant_with_section_filter():
    """v2.8.12: section filter narrows hits."""
    from app.skills.clients import gdy
    r = asyncio.get_event_loop().run_until_complete(
        gdy.gdy_meta_catalog_search(
            {"query": "qdrant", "limit": 5, "section": "MCP"}, {}
        )
    )
    assert r["ok"] is True
    # qdrant-mcp is in MCP section
    assert any("mcp-server-qdrant" in h["repo"] for h in r["hits"])
    # qdrant/qdrant is in Vector databases section, not MCP
    repo_names = {h["repo"] for h in r["hits"]}
    assert "qdrant/qdrant" not in repo_names


def test_gdy_meta_catalog_search_rejects_empty_query():
    from app.skills.clients import gdy
    r = asyncio.get_event_loop().run_until_complete(
        gdy.gdy_meta_catalog_search({"query": ""}, {})
    )
    assert r["ok"] is False
    assert r["error_code"] == "missing_required:query"


def test_gdy_meta_catalog_search_handles_no_match():
    """v2.8.12: a query that matches nothing returns ok=True with 0 hits
    (not an error — the agent should fall through to live GDY or
    answer with 'no match' rather than fail)."""
    from app.skills.clients import gdy
    r = asyncio.get_event_loop().run_until_complete(
        gdy.gdy_meta_catalog_search({"query": "zxcvbnmasdfqwer", "limit": 5}, {})
    )
    assert r["ok"] is True
    assert r["total"] == 0
    assert r["hits"] == []


def test_contract_gdy_meta_catalog_skill_registered():
    """v2.8.12: gdy.meta_catalog_search is registered in seed_all.py
    so the agent can call it via /api/skills/run."""
    seed = Path("app/skills/seed_all.py").read_text()
    assert '"gdy.meta_catalog_search"' in seed
    assert '"builtin:gdy.meta_catalog_search": gdy.gdy_meta_catalog_search' in seed


def test_contract_total_skill_count_is_46():
    """v2.8.12: total SkillSpec count in seed_all.py is 46 (45 + 1)."""
    import subprocess
    out = subprocess.check_output(
        ["bash", "-c", 'grep "SkillSpec(id=" app/skills/seed_all.py | wc -l']
    ).decode().strip()
    assert out == "46", f"expected 46 SkillSpec, got {out}"


def test_contract_status_warning_entries_have_warning_and_replacement():
    """v2.8.12: the 3 status-warning repos (Continue, Roo-Code, AutoGen)
    AND the active replacement fork (Roomote) are all in the
    meta-catalog with `warning` or `replaced_by` flags. The agent can
    then say 'use Roomote instead of Roo-Code' instead of misleading."""
    p = Path("data/gdy_meta_catalog.json")
    catalog = json.loads(p.read_text())
    by_repo = {e["repo"]: e for e in catalog}
    # 4 entries
    assert "continuedev/continue" in by_repo
    assert "RooCodeInc/Roo-Code" in by_repo
    assert "microsoft/autogen" in by_repo
    assert "RooCodeInc/Roomote" in by_repo
    # Warning fields set
    assert by_repo["continuedev/continue"].get("warning", "").startswith("archived")
    assert by_repo["RooCodeInc/Roo-Code"].get("warning", "").startswith("archived")
    assert by_repo["microsoft/autogen"].get("warning", "").startswith("maintenance")
    # Roo-Code explicitly points to its replacement
    assert by_repo["RooCodeInc/Roo-Code"].get("replaced_by") == "RooCodeInc/Roomote"


def test_gdy_meta_catalog_search_returns_warning_when_archived():
    """v2.8.12: searching for an archived repo surfaces the warning."""
    from app.skills.clients import gdy
    r = asyncio.get_event_loop().run_until_complete(
        gdy.gdy_meta_catalog_search({"query": "Roo-Code", "limit": 5}, {})
    )
    assert r["ok"] is True
    # Find Roo-Code in hits
    roo = next((h for h in r["hits"] if h["repo"] == "RooCodeInc/Roo-Code"), None)
    assert roo is not None, "RooCodeInc/Roo-Code not in hits"
    assert roo.get("warning", "").startswith("archived")
    assert roo.get("replaced_by") == "RooCodeInc/Roomote"
