"""Seed full skill catalog + wire all executors."""
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
        SkillSpec(
            id="web.search",
            name="Web search",
            description="Search via Tavily or Exa (env).",
            side_effect="network",
            input_schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}, "count": {"type": "integer"}}},
            executor="builtin:web.search",
            tags=["search"],
            error_codes=["web_search_not_configured", "web_search_http_error", "invalid_args"],
        ),
        SkillSpec(
            id="github.repo",
            name="GitHub repo",
            description="Fetch allowlisted repository metadata.",
            side_effect="network",
            input_schema={"type": "object", "required": ["repository"], "properties": {"repository": {"type": "string"}}},
            executor="builtin:github.repo",
            tags=["github"],
            error_codes=["github_not_configured", "github_repository_not_allowed", "github_http_error", "invalid_github_repository"],
        ),
        SkillSpec(
            id="github.file",
            name="GitHub file",
            description="Read file from allowlisted repo.",
            side_effect="network",
            input_schema={"type": "object", "required": ["repository", "path"], "properties": {"repository": {"type": "string"}, "path": {"type": "string"}, "ref": {"type": "string"}}},
            executor="builtin:github.file",
            tags=["github"],
            error_codes=["github_not_configured", "github_repository_not_allowed", "github_not_found"],
        ),
        SkillSpec(
            id="github.issues",
            name="GitHub issues",
            description="List open issues.",
            side_effect="network",
            input_schema={"type": "object", "required": ["repository"], "properties": {"repository": {"type": "string"}}},
            executor="builtin:github.issues",
            tags=["github"],
            error_codes=["github_not_configured", "github_repository_not_allowed"],
        ),
        SkillSpec(
            id="github.search",
            name="GitHub code search",
            description="Search code in allowlisted repo.",
            side_effect="network",
            input_schema={"type": "object", "required": ["repository", "query"], "properties": {"repository": {"type": "string"}, "query": {"type": "string"}, "limit": {"type": "integer"}}},
            executor="builtin:github.search",
            tags=["github"],
            error_codes=["github_not_configured", "github_repository_not_allowed"],
        ),
        SkillSpec(
            id="scrape.url",
            name="Scrape URL",
            description="Scrape page via Firecrawl/ScrapingBee/Scrapfly.",
            side_effect="network",
            input_schema={"type": "object", "required": ["url"], "properties": {"url": {"type": "string"}}},
            executor="builtin:scrape.url",
            tags=["scrape"],
            error_codes=["scrape_not_configured", "scrape_http_error", "invalid_args"],
            timeout_ms=60_000,
        ),
        SkillSpec(
            id="email.send",
            name="Send email",
            description="Send email via Resend.",
            side_effect="write",
            input_schema={"type": "object", "required": ["to", "subject"], "properties": {"to": {}, "subject": {"type": "string"}, "html": {"type": "string"}, "from": {"type": "string"}}},
            executor="builtin:email.send",
            tags=["email", "write"],
            error_codes=["skill_not_configured", "email_send_failed", "invalid_args"],
        ),
        SkillSpec(
            id="rag.skills.search",
            name="RAG skills search",
            description="Keyword RAG over skills collection.",
            side_effect="read",
            input_schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}},
            executor="builtin:rag.skills.search",
            tags=["rag", "skills"],
            error_codes=["rag_empty", "invalid_args"],
        ),
        SkillSpec(
            id="rag.code.search",
            name="RAG code search",
            description="Keyword RAG over code collection.",
            side_effect="read",
            input_schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}},
            executor="builtin:rag.code.search",
            tags=["rag", "code"],
            error_codes=["rag_empty", "invalid_args"],
        ),
        SkillSpec(
            id="rag.upsert",
            name="RAG upsert",
            description="Upsert text into skills|code|docs collection.",
            side_effect="write",
            input_schema={"type": "object", "required": ["text"], "properties": {"collection": {"type": "string"}, "text": {"type": "string"}, "source": {"type": "string"}}},
            executor="builtin:rag.upsert",
            tags=["rag", "write"],
            error_codes=["invalid_args"],
        ),
        SkillSpec(
            id="rag.index_catalog",
            name="Index skill catalog into RAG",
            description="Subroutine: write skill catalog into skills RAG collection.",
            side_effect="write",
            input_schema={"type": "object", "properties": {}},
            executor="builtin:rag.index_catalog",
            tags=["rag", "meta"],
            error_codes=[],
        ),
        SkillSpec(
            id="skills.catalog",
            name="List skills",
            description="Return enabled skill contracts.",
            side_effect="none",
            input_schema={"type": "object", "properties": {}},
            executor="builtin:skills.catalog",
            tags=["meta"],
            error_codes=[],
        ),
        SkillSpec(
            id="github.scenario.match",
            name="GitHub scenario match",
            description="Lookup the policy CSV for the trigger that just happened. Returns ranked matches from $AION_DATA_DIR/github_scenarios.csv with if_action / else_action / severity / source_doc. Never invents a row.",
            side_effect="read",
            input_schema={
                "type": "object",
                "required": ["trigger"],
                "properties": {
                    "trigger": {"type": "string"},
                    "condition": {"type": "string"},
                    "category": {"type": "string"},
                    "context": {"type": "object"},
                    "csv_path": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
            executor="builtin:github.scenario.match",
            tags=["github", "policy", "read"],
            error_codes=[
                "github_scenarios_load_failed",
                "github_scenarios_empty",
                "invalid_args",
            ],
        ),
        SkillSpec(
            id="github.scenario.index",
            name="GitHub scenario index",
            description="Subroutine: index the policy CSV into the local RAG 'github_policy' collection so rag.skills.search can find scenarios by natural language.",
            side_effect="write",
            input_schema={
                "type": "object",
                "properties": {"csv_path": {"type": "string"}},
            },
            executor="builtin:github.scenario.index",
            tags=["github", "policy", "rag"],
            error_codes=["github_scenarios_load_failed", "github_scenarios_empty"],
        ),
    ]


def wire_executors() -> None:
    from .clients import github_scenarios as ghs
    r = get_runner()
    r.register_many(
        {
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
        }
    )


async def _catalog(args, ctx):
    reg = get_registry()
    return {"skills": reg.catalog(), "count": len(reg.list())}


def bootstrap() -> dict:
    reg = get_registry()
    n = reg.seed(all_specs())
    wire_executors()
    return {"seeded": n, "skills": [s.id for s in reg.list()], "db": reg.db_path}


if __name__ == "__main__":
    import json
    print(json.dumps(bootstrap(), indent=2))
