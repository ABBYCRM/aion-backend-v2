"""Seed the skill registry with main AION micro-skills.

These map to real executors on aion-backend-v2 (tools.py, notes, vault, etc.).
Ids are stable; the runner dispatches by id / executor key.
"""
from __future__ import annotations

from .skills_db import SkillSpec, SkillRegistry, get_registry


def builtin_skills() -> list[SkillSpec]:
    return [
        SkillSpec(
            id="web.search",
            name="Web search",
            description="Search the public web; returns structured hits or hard error.",
            version="1.0.0",
            side_effect="network",
            input_schema={
                "type": "object",
                "required": ["query"],
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "count": {"type": "integer", "description": "Max results"},
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "results": {"type": "array"},
                    "count": {"type": "integer"},
                },
            },
            timeout_ms=20_000,
            executor="builtin:web.search",
            tags=["search", "network", "read"],
            error_codes=[
                "web_search_not_configured",
                "web_search_http_error",
                "web_search_empty",
            ],
        ),
        SkillSpec(
            id="github.repo",
            name="GitHub repository summary",
            description="Fetch metadata for an allowlisted owner/repo.",
            version="1.0.0",
            side_effect="network",
            input_schema={
                "type": "object",
                "required": ["repository"],
                "additionalProperties": False,
                "properties": {
                    "repository": {
                        "type": "string",
                        "description": "owner/repo",
                    },
                },
            },
            output_schema={"type": "object"},
            timeout_ms=20_000,
            executor="builtin:github.repo",
            tags=["github", "read"],
            error_codes=[
                "github_not_configured",
                "github_repository_not_allowed",
                "invalid_github_repository",
                "github_http_error",
            ],
        ),
        SkillSpec(
            id="github.file",
            name="GitHub file read",
            description="Read a file from an allowlisted repository.",
            version="1.0.0",
            side_effect="network",
            input_schema={
                "type": "object",
                "required": ["repository", "path"],
                "additionalProperties": False,
                "properties": {
                    "repository": {"type": "string"},
                    "path": {"type": "string"},
                    "ref": {"type": "string"},
                },
            },
            output_schema={"type": "object"},
            timeout_ms=20_000,
            executor="builtin:github.file",
            tags=["github", "read"],
            error_codes=[
                "github_not_configured",
                "github_repository_not_allowed",
                "github_file_not_found",
                "github_http_error",
            ],
        ),
        SkillSpec(
            id="github.issues",
            name="GitHub list issues",
            description="List issues for an allowlisted repository.",
            version="1.0.0",
            side_effect="network",
            input_schema={
                "type": "object",
                "required": ["repository"],
                "additionalProperties": False,
                "properties": {
                    "repository": {"type": "string"},
                },
            },
            output_schema={"type": "object"},
            timeout_ms=20_000,
            executor="builtin:github.issues",
            tags=["github", "read"],
            error_codes=[
                "github_not_configured",
                "github_repository_not_allowed",
                "github_http_error",
            ],
        ),
        SkillSpec(
            id="github.search",
            name="GitHub code search",
            description="Search code inside an allowlisted repository.",
            version="1.0.0",
            side_effect="network",
            input_schema={
                "type": "object",
                "required": ["repository", "query"],
                "additionalProperties": False,
                "properties": {
                    "repository": {"type": "string"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
            output_schema={"type": "object"},
            timeout_ms=25_000,
            executor="builtin:github.search",
            tags=["github", "read"],
            error_codes=[
                "github_not_configured",
                "github_repository_not_allowed",
                "github_http_error",
            ],
        ),
        SkillSpec(
            id="notes.context",
            name="Notes context",
            description="Load operator notes relevant to a query (opt-in).",
            version="1.0.0",
            side_effect="read",
            input_schema={
                "type": "object",
                "required": ["subject"],
                "properties": {
                    "subject": {"type": "string"},
                    "query": {"type": "string"},
                },
            },
            output_schema={"type": "object"},
            timeout_ms=5_000,
            executor="builtin:notes.context",
            tags=["notes", "read"],
            error_codes=["notes_disabled", "notes_error"],
        ),
        SkillSpec(
            id="vault.get",
            name="Vault get",
            description="Read a named secret from the vault (admin/policy gated).",
            version="1.0.0",
            side_effect="admin",
            input_schema={
                "type": "object",
                "required": ["name"],
                "additionalProperties": False,
                "properties": {"name": {"type": "string"}},
            },
            output_schema={"type": "object"},
            timeout_ms=5_000,
            executor="builtin:vault.get",
            tags=["vault", "admin"],
            error_codes=["vault_denied", "vault_not_found", "vault_error"],
        ),
        SkillSpec(
            id="skills.catalog",
            name="List skills",
            description="Return the enabled skill catalog (no execution of other skills).",
            version="1.0.0",
            side_effect="none",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object"},
            timeout_ms=2_000,
            executor="builtin:skills.catalog",
            tags=["meta"],
            error_codes=[],
        ),
    ]


def seed_registry(registry: SkillRegistry | None = None) -> int:
    reg = registry or get_registry()
    return reg.seed(builtin_skills())


if __name__ == "__main__":
    n = seed_registry()
    print(f"seeded {n} skills into {get_registry().db_path}")
    for s in get_registry().list():
        print(f"  {s.id:20} {s.side_effect:8} {s.name}")
