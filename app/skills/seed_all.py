"""Seed the skill registry with the full 20-skill pack on AION boot.

The 12 original skills are the v1 full pack (web search, github,
scrape, email, RAG, catalog). The 8 new skills are the operator's
scenario policy library:
  - github.scenario.match / github.scenario.index
  - openclaw.scenario.match
  - composio.scenario.match
  - firecrawl_steel.scenario.match
  - render.scenario.match
  - scenario.match  (unified, all 5 packs at once)
  - scenario.index  (RAG indexer, 1 pack or all)

Total: 20 contracts on boot.
"""
from __future__ import annotations

from .registry_core import SkillSpec, get_registry
from .runner import get_runner
from .clients import web_search as web
from .clients import github_sk as gh
from .clients import scrape
from .clients import email_resend
from .rag import skills_rag as rag


def all_specs() -> list[SkillSpec]:
    return [
        SkillSpec(id="web.search", name="Web search",
            description="Search the web via Tavily, Exa, or DDG fallback. Returns ranked results.",
            side_effect="network",
            input_schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}},
            executor="builtin:web.search", tags=["search", "network"],
            error_codes=["web_search_not_configured", "web_search_http_error"]),
        SkillSpec(id="github.repo", name="GitHub repo info",
            description="Fetch repository metadata (stars, language, license, default_branch).",
            side_effect="network",
            input_schema={"type": "object", "required": ["repository"], "properties": {"repository": {"type": "string"}}},
            executor="builtin:github.repo", tags=["github", "network"],
            error_codes=["github_not_configured", "github_http_error", "github_repository_not_allowed"]),
        SkillSpec(id="github.file", name="GitHub file content",
            description="Fetch a file from a repository. Returns raw text or base64 for binaries.",
            side_effect="network",
            input_schema={"type": "object", "required": ["repository", "path"], "properties": {"repository": {"type": "string"}, "path": {"type": "string"}, "ref": {"type": "string"}}},
            executor="builtin:github.file", tags=["github", "network"],
            error_codes=["github_not_configured", "github_http_error", "github_repository_not_allowed"]),
        SkillSpec(id="github.issues", name="GitHub issues list",
            description="List open issues for a repository.",
            side_effect="network",
            input_schema={"type": "object", "required": ["repository"], "properties": {"repository": {"type": "string"}, "state": {"type": "string"}}},
            executor="builtin:github.issues", tags=["github", "network"],
            error_codes=["github_not_configured", "github_http_error", "github_repository_not_allowed"]),
        SkillSpec(id="github.search", name="GitHub code search",
            description="Search code/issues in a single repository via the GitHub Search API.",
            side_effect="network",
            input_schema={"type": "object", "required": ["repository", "query"], "properties": {"repository": {"type": "string"}, "query": {"type": "string"}}},
            executor="builtin:github.search", tags=["github", "network"],
            error_codes=["github_not_configured", "github_http_error", "github_repository_not_allowed"]),
        SkillSpec(id="scrape.url", name="Scrape URL",
            description="Scrape a URL into markdown via Firecrawl, ScrapingBee, or Scrapfly.",
            side_effect="network",
            input_schema={"type": "object", "required": ["url"], "properties": {"url": {"type": "string"}}},
            executor="builtin:scrape.url", tags=["scrape", "network"],
            error_codes=["scrape_not_configured", "scrape_http_error"]),
        SkillSpec(id="email.send", name="Send email",
            description="Send transactional email via Resend. Required: to, subject, html.",
            side_effect="write",
            input_schema={"type": "object", "required": ["to", "subject", "html"], "properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "html": {"type": "string"}, "from": {"type": "string"}}},
            executor="builtin:email.send", tags=["email", "write"],
            error_codes=["email_send_failed", "skill_not_configured"]),
        SkillSpec(id="rag.skills.search", name="RAG search (skills)",
            description="Search the local RAG index for skill content (catalogs, docs, etc.).",
            side_effect="read",
            input_schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}, "collection": {"type": "string"}, "limit": {"type": "integer"}}},
            executor="builtin:rag.skills.search", tags=["rag", "read"],
            error_codes=["rag_empty"]),
        SkillSpec(id="rag.code.search", name="RAG search (code)",
            description="Search the local code RAG index.",
            side_effect="read",
            input_schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}},
            executor="builtin:rag.code.search", tags=["rag", "read"],
            error_codes=["rag_empty"]),
        SkillSpec(id="rag.upsert", name="RAG upsert",
            description="Add a chunk to the local RAG index.",
            side_effect="write",
            input_schema={"type": "object", "required": ["collection", "text"], "properties": {"collection": {"type": "string"}, "text": {"type": "string"}, "source": {"type": "string"}, "meta": {"type": "object"}}},
            executor="builtin:rag.upsert", tags=["rag", "write"],
            error_codes=[]),
        SkillSpec(id="rag.index_catalog", name="RAG index catalog",
            description="Index all skills' descriptions into the skills RAG collection.",
            side_effect="write",
            input_schema={"type": "object", "properties": {}},
            executor="builtin:rag.index_catalog", tags=["rag", "write"],
            error_codes=[]),
        SkillSpec(id="skills.catalog", name="List skills",
            description="Return enabled skill contracts.",
            side_effect="none",
            input_schema={"type": "object", "properties": {}},
            executor="builtin:skills.catalog", tags=["meta"],
            error_codes=[]),
        # ---- 5 scenario matchers + 1 unified + 1 index = 7 new + 1 legacy = 8 ----
        SkillSpec(id="github.scenario.match", name="GitHub scenario match",
            description="Lookup the GitHub policy CSV. 500 rows grounded in 20+ GitHub docs pages (actions, secrets, API, webhooks, branch, Dependabot, Pages, etc.). Returns ranked matches with if_action / else_action / severity / source_doc. Never invents a row.",
            side_effect="read",
            input_schema={"type": "object", "required": ["trigger"], "properties": {"trigger": {"type": "string"}, "condition": {"type": "string"}, "category": {"type": "string"}, "context": {"type": "object"}, "limit": {"type": "integer"}}},
            executor="builtin:github.scenario.match", tags=["github", "policy", "read"],
            error_codes=["scenarios_load_failed", "scenarios_empty", "invalid_args"]),
        SkillSpec(id="openclaw.scenario.match", name="OpenClaw scenario match",
            description="Lookup the OpenClaw policy CSV. 500 rows covering shell, filesystem, Gmail, Notion, Slack, browser, skills. Never invents a row.",
            side_effect="read",
            input_schema={"type": "object", "required": ["trigger"], "properties": {"trigger": {"type": "string"}, "condition": {"type": "string"}, "category": {"type": "string"}, "context": {"type": "object"}, "limit": {"type": "integer"}}},
            executor="builtin:openclaw.scenario.match", tags=["openclaw", "policy", "read"],
            error_codes=["scenarios_load_failed", "scenarios_empty", "invalid_args"]),
        SkillSpec(id="composio.scenario.match", name="Composio scenario match",
            description="Lookup the Composio policy CSV. 500 rows covering sessions, auth, connected accounts, tool execute, MCP. Never invents a row.",
            side_effect="read",
            input_schema={"type": "object", "required": ["trigger"], "properties": {"trigger": {"type": "string"}, "condition": {"type": "string"}, "category": {"type": "string"}, "context": {"type": "object"}, "limit": {"type": "integer"}}},
            executor="builtin:composio.scenario.match", tags=["composio", "policy", "read"],
            error_codes=["scenarios_load_failed", "scenarios_empty", "invalid_args"]),
        SkillSpec(id="firecrawl_steel.scenario.match", name="Firecrawl/Steel scenario match",
            description="Lookup the Firecrawl+Steel policy CSV. 500 rows covering scrape, crawl, map, search, Steel sessions, pipeline handoff. Never invents a row.",
            side_effect="read",
            input_schema={"type": "object", "required": ["trigger"], "properties": {"trigger": {"type": "string"}, "condition": {"type": "string"}, "category": {"type": "string"}, "context": {"type": "object"}, "limit": {"type": "integer"}}},
            executor="builtin:firecrawl_steel.scenario.match", tags=["firecrawl", "steel", "policy", "read"],
            error_codes=["scenarios_load_failed", "scenarios_empty", "invalid_args"]),
        SkillSpec(id="render.scenario.match", name="Render scenario match",
            description="Lookup the Render policy CSV. 500 rows covering build, boot, health, runtime, scaling, env, API, DB, rollback. Never invents a row.",
            side_effect="read",
            input_schema={"type": "object", "required": ["trigger"], "properties": {"trigger": {"type": "string"}, "condition": {"type": "string"}, "category": {"type": "string"}, "context": {"type": "object"}, "limit": {"type": "integer"}}},
            executor="builtin:render.scenario.match", tags=["render", "policy", "read"],
            error_codes=["scenarios_load_failed", "scenarios_empty", "invalid_args"]),
        SkillSpec(id="scenario.match", name="Scenario match (all packs)",
            description="Unified scenario matcher: search all 5 packs (github, openclaw, composio, firecrawl_steel, render) at once. Returns ranked matches across packs so the model can pick the right one. Use a per-pack skill (e.g. github.scenario.match) when you know which domain the error is in.",
            side_effect="read",
            input_schema={"type": "object", "required": ["trigger"], "properties": {"trigger": {"type": "string"}, "condition": {"type": "string"}, "category": {"type": "string"}, "context": {"type": "object"}, "limit": {"type": "integer"}}},
            executor="builtin:scenario.match", tags=["policy", "read", "all-packs"],
            error_codes=["scenarios_load_failed", "scenarios_empty", "invalid_args"]),
        SkillSpec(id="scenario.index", name="Scenario index (RAG)",
            description="Index one or all scenario packs into the local RAG so rag.skills.search can find rows by natural language. Default collection: scenario_policy. Per-pack collection: scenario_policy_<pack>.",
            side_effect="write",
            input_schema={"type": "object", "properties": {"pack": {"type": "string", "enum": ["github", "openclaw", "composio", "firecrawl_steel", "render", "all"]}}},
            executor="builtin:scenario.index", tags=["policy", "rag", "write"],
            error_codes=["scenarios_load_failed", "scenarios_empty", "invalid_args"]),
        SkillSpec(id="github.scenario.index", name="GitHub scenario index (legacy)",
            description="Legacy: indexes the github pack into the 'github_policy' collection. Use scenario.index pack=github for the new behavior.",
            side_effect="write",
            input_schema={"type": "object", "properties": {}},
            executor="builtin:github.scenario.index", tags=["github", "policy", "rag", "legacy"],
            error_codes=["scenarios_load_failed", "scenarios_empty"]),
        # ---- 3 coding-books skills (operator RAG pack, 2026-08-05) ----
        SkillSpec(id="coding.books.index", name="Coding books RAG indexer",
            description="Parse data/books/coding_books_catalog.json (39 open coding books) and upsert one RAG document per book into the coding_books collection. No PDF download required; indexes metadata + notes. Idempotent (deterministic chunk_id per book).",
            side_effect="write",
            input_schema={"type": "object", "properties": {"catalog_path": {"type": "string"}}, "additionalProperties": True},
            executor="builtin:coding.books.index", tags=["rag", "books", "index"],
            error_codes=["catalog_not_found", "index_failed", "rag_store_unavailable"]),
        SkillSpec(id="coding.books.search", name="Coding books RAG search",
            description="Keyword search the coding_books RAG collection. Returns ranked books with id/title/license/url_primary.",
            side_effect="read",
            input_schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}},
            executor="builtin:coding.books.search", tags=["rag", "books", "search"],
            error_codes=["invalid_args", "rag_search_unavailable"]),
        SkillSpec(id="coding.books.catalog", name="Coding books catalog filter",
            description="List/filter the coding_books catalog without embeddings. Filters: topic, level, language. Returns id/title/level/topics/languages/url_primary/url_pdf/license.",
            side_effect="read",
            input_schema={"type": "object", "properties": {"topic": {"type": "string"}, "level": {"type": "string"}, "language": {"type": "string"}, "catalog_path": {"type": "string"}}, "additionalProperties": True},
            executor="builtin:coding.books.catalog", tags=["books", "catalog"],
            error_codes=["catalog_not_found"]),
    ]


async def _catalog(args, ctx):
    """Return the enabled skill contracts from the registry."""
    reg = get_registry()
    skills = reg.list(enabled_only=True)
    return {
        "count": len(skills),
        "skills": [
            {
                "id": s.id, "name": s.name, "description": s.description,
                "version": s.version, "side_effect": s.side_effect,
                "input_schema": s.input_schema, "executor": s.executor,
                "tags": list(s.tags), "error_codes": list(s.error_codes),
            }
            for s in skills
        ],
    }


def wire_executors() -> None:
    from .clients import github_scenarios as ghs
    from .clients import scenarios as scn
    from .clients import coding_books_rag as cbr
    r = get_runner()
    r.register_many({
        "builtin:web.search": web.web_search,
        "builtin:github.repo": gh.github_repo,
        "builtin:github.file": gh.github_file,
        "builtin:github.issues": gh.github_issues,
        "builtin:github.search": gh.github_search,
        "builtin:scrape.url": scrape.scrape_url,
        "builtin:email.send": email_resend.email_send,
        "builtin:rag.skills.search": rag.rag_skills_search,
        "builtin:rag.code.search": rag.rag_code_search,
        "builtin:rag.upsert": rag.rag_upsert,
        "builtin:rag.index_catalog": rag.rag_index_skill_catalog,
        "builtin:skills.catalog": _catalog,
        "builtin:github.scenario.match": ghs.github_scenario_match,
        "builtin:github.scenario.index": ghs.github_scenario_index,
        "builtin:openclaw.scenario.match": scn.openclaw_scenario_match,
        "builtin:composio.scenario.match": scn.composio_scenario_match,
        "builtin:firecrawl_steel.scenario.match": scn.firecrawl_steel_scenario_match,
        "builtin:render.scenario.match": scn.render_scenario_match,
        "builtin:scenario.match": scn.scenario_match_all,
        "builtin:scenario.index": scn.scenario_index,
        "builtin:coding.books.index": cbr.coding_books_index,
        "builtin:coding.books.search": cbr.coding_books_search,
        "builtin:coding.books.catalog": cbr.coding_books_catalog,
    })


def bootstrap() -> dict:
    """Idempotent seed + executor wiring. Safe to call on every boot."""
    reg = get_registry()
    wire_executors()
    n = reg.seed(all_specs())
    return {"seeded": n, "db": reg.db_path}
