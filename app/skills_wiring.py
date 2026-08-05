"""Wire skill executors to real AION tools.

Each executor is an async (args, ctx) -> dict. When the tool raises a
ToolRequestError, the runner surfaces the error code; otherwise the dict
returned is wrapped in the skill result.

The point of having a registry at all: the model cannot fabricate a
github.repo payload. If the args are wrong, the registry says so; if
the executor isn't wired, the runner says so; if the tool call fails,
the tool's error code is propagated verbatim. There is no path for
the model to invent a repository review.
"""
from __future__ import annotations

from typing import Any

from . import tools
from . import notes as notes_module
from . import vault as vault_module
from .tools import ToolRequestError


def _bail(msg: str, code: str) -> None:
    """Raise a ToolRequestError with a stable error code for the runner."""
    raise ToolRequestError(f"{code}: {msg}")


async def _executor_web_search(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    query = args.get("query", "").strip()
    if not query:
        _bail("query required", "web_search_invalid_query")
    count = int(args.get("count") or 5)
    try:
        results = await tools.web_search.search(query, count=count)
    except Exception as exc:
        # web_search.search already raises ToolRequestError; let it through.
        raise
    return {
        "query": query,
        "count": len(results),
        "results": [r.__dict__ for r in results],
    }


async def _executor_github_repo(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    repository = args.get("repository", "").strip()
    if not repository:
        _bail("repository required", "invalid_github_repository")
    return await tools.github.get_repository(repository)


async def _executor_github_file(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    repository = args.get("repository", "").strip()
    path = args.get("path", "").strip()
    if not repository or not path:
        _bail("repository and path required", "invalid_github_file_args")
    ref = args.get("ref")
    return await tools.github.get_file(repository, path, ref)


async def _executor_github_issues(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    repository = args.get("repository", "").strip()
    if not repository:
        _bail("repository required", "invalid_github_repository")
    items = await tools.github.list_issues(repository)
    return {"items": items, "count": len(items)}


async def _executor_github_search(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    repository = args.get("repository", "").strip()
    query = args.get("query", "").strip()
    if not repository or not query:
        _bail("repository and query required", "invalid_github_search_args")
    limit = int(args.get("limit") or 10)
    items = await tools.github.search_code(repository, query, limit=limit)
    return {"items": items, "count": len(items)}


async def _executor_notes_context(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    subject = args.get("subject", "").strip() or ctx.get("subject", "")
    if not subject:
        _bail("subject required", "notes_no_subject")
    query = args.get("query", "")
    text = notes_module.context(subject, query)
    return {"context": text, "subject": subject}


async def _executor_vault_get(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    name = args.get("name", "").strip()
    if not name:
        _bail("name required", "vault_no_name")
    # Admin gate at the route level; this executor just does the read.
    value = vault_module.reveal(name)
    if value is None:
        _bail(f"not found: {name}", "vault_not_found")
    from .settings import settings
    # Build a fingerprint + length, never return the secret plaintext in the dict.
    # The plaintext is allowed only when the caller is admin AND we are explicit.
    reveal = bool(ctx.get("reveal"))
    if not reveal:
        return {
            "name": name,
            "has_value": True,
            "value_length": len(value) if value else 0,
            "fingerprint": vault_module._fingerprint(value) if value else "",
        }
    # Admin explicitly asked for the secret. This is the only path that
    # returns plaintext; tracked via the audit log separately.
    return {"name": name, "value": value}


async def _executor_skills_catalog(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    from .skills_db import get_registry
    reg = get_registry()
    return {
        "count": len(reg.list()),
        "skills": reg.catalog(),
    }


# Mapping: builtin key -> async (args, ctx) -> dict
BUILTIN_EXECUTORS: dict[str, Any] = {
    "builtin:web.search": _executor_web_search,
    "builtin:github.repo": _executor_github_repo,
    "builtin:github.file": _executor_github_file,
    "builtin:github.issues": _executor_github_issues,
    "builtin:github.search": _executor_github_search,
    "builtin:notes.context": _executor_notes_context,
    "builtin:vault.get": _executor_vault_get,
    "builtin:skills.catalog": _executor_skills_catalog,
}


def register_all_executors() -> int:
    """Wire all built-in executors into the singleton runner.

    Idempotent. Returns the number of executors registered.
    """
    from .skills_runner import get_runner
    runner = get_runner()
    runner.register_many(BUILTIN_EXECUTORS)
    return len(BUILTIN_EXECUTORS)
