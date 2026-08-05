"""RAG skills: skills docs + code-ish collections via LocalRagStore."""
from __future__ import annotations

from typing import Any

from ..base import SkillError
from .store import get_rag_store


async def rag_skills_search(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    if not query:
        raise SkillError("invalid_args", "missing_required:query")
    limit = int(args.get("limit") or 5)
    store = get_rag_store()
    hits = store.search("skills", query, limit=limit)
    if not hits and store.count("skills") == 0:
        raise SkillError("rag_empty", "skills_collection_empty")
    return {"collection": "skills", "query": query, "count": len(hits), "hits": hits}


async def rag_code_search(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    if not query:
        raise SkillError("invalid_args", "missing_required:query")
    limit = int(args.get("limit") or 5)
    store = get_rag_store()
    hits = store.search("code", query, limit=limit)
    if not hits and store.count("code") == 0:
        raise SkillError("rag_empty", "code_collection_empty")
    return {"collection": "code", "query": query, "count": len(hits), "hits": hits}


async def rag_upsert(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    collection = (args.get("collection") or "skills").strip()
    text = (args.get("text") or "").strip()
    source = (args.get("source") or "").strip()
    if collection not in ("skills", "code", "docs"):
        raise SkillError("invalid_args", "collection_must_be_skills|code|docs")
    if not text:
        raise SkillError("invalid_args", "missing_required:text")
    # chunk large text
    chunks = _chunk(text, max_len=1200)
    store = get_rag_store()
    ids = []
    for i, ch in enumerate(chunks):
        cid = store.upsert(collection, ch, source=source or f"chunk-{i}", meta={"i": i})
        ids.append(cid)
    return {"collection": collection, "upserted": len(ids), "ids": ids}


def _chunk(text: str, max_len: int = 1200) -> list[str]:
    text = text.strip()
    if len(text) <= max_len:
        return [text]
    parts = []
    start = 0
    while start < len(text):
        parts.append(text[start : start + max_len])
        start += max_len
    return parts


async def rag_index_skill_catalog(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Subroutine: index public skill catalog into skills collection."""
    from ..registry_core import get_registry

    catalog = get_registry().catalog(enabled_only=True)
    store = get_rag_store()
    n = 0
    for s in catalog:
        text = f"{s['id']}\n{s['name']}\n{s.get('description')}\ntags:{','.join(s.get('tags') or [])}\nerrors:{','.join(s.get('error_codes') or [])}"
        store.upsert("skills", text, source=s["id"], meta={"skill_id": s["id"]})
        n += 1
    return {"indexed": n, "collection": "skills"}
