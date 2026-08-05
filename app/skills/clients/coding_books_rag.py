"""
AION coding-books catalog parser + RAG indexer.

Loads data/books/coding_books_catalog.json (or data/coding_books_catalog.json),
turns each book into searchable text chunks, upserts into collection `coding_books`
via the existing RAG store / rag.upsert skill path.

No PDF download required for MVP: metadata + titles + topics + notes are indexed.
Optional later: fetch url_pdf / html and chunk body text.
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("aion.skills.coding_books_rag")

COLLECTION = "coding_books"
CATALOG_NAMES = (
    "coding_books_catalog.json",
    "books/coding_books_catalog.json",
)


def _data_roots() -> list[Path]:
    roots: list[Path] = []
    for key in ("AION_DATA_DIR", "AION_BOOKS_DIR"):
        v = os.environ.get(key)
        if v:
            roots.append(Path(v))
            roots.append(Path(v) / "books")
    here = Path(__file__).resolve()
    roots.extend(
        [
            Path.cwd() / "data",
            Path.cwd() / "data" / "books",
            here.parents[3] / "data" if len(here.parents) > 3 else Path.cwd() / "data",
            here.parents[3] / "data" / "books" if len(here.parents) > 3 else Path.cwd() / "data" / "books",
            Path("/app/data"),
            Path("/app/data/books"),
        ]
    )
    # dedupe preserve order
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        s = str(r)
        if s not in seen:
            seen.add(s)
            out.append(r)
    return out


def resolve_catalog_path(explicit: str | None = None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise FileNotFoundError(f"catalog not found: {p}")
        return p
    for root in _data_roots():
        for name in CATALOG_NAMES:
            cand = root / name if not name.startswith("books/") else root.parent / name if root.name == "books" else root / name
            # simpler: try root / name for both patterns
        for name in ("coding_books_catalog.json",):
            cand = root / name
            if cand.is_file():
                return cand
        cand = root / "books" / "coding_books_catalog.json"
        if cand.is_file():
            return cand
    raise FileNotFoundError(
        "coding_books_catalog.json not found under data/ or $AION_DATA_DIR. "
        "Copy package data/coding_books_catalog.json to data/books/."
    )


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    p = path or resolve_catalog_path()
    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or "books" not in data:
        raise ValueError("catalog must be an object with a 'books' array")
    return data


def book_to_document(book: dict[str, Any]) -> dict[str, Any]:
    """One RAG document per book (metadata-rich)."""
    authors = ", ".join(book.get("authors") or [])
    topics = ", ".join(book.get("topics") or [])
    langs = ", ".join(book.get("languages") or [])
    formats = ", ".join(book.get("formats") or [])
    text = (
        f"BOOK: {book.get('title')}\n"
        f"ID: {book.get('id')}\n"
        f"Authors: {authors}\n"
        f"Year: {book.get('year')}\n"
        f"Level: {book.get('level')}\n"
        f"Topics: {topics}\n"
        f"Languages: {langs}\n"
        f"Formats: {formats}\n"
        f"License: {book.get('license')}\n"
        f"Source: {book.get('source')}\n"
        f"Primary URL: {book.get('url_primary')}\n"
        f"PDF URL: {book.get('url_pdf')}\n"
        f"Notes: {book.get('notes') or ''}\n"
    )
    return {
        "id": f"book:{book.get('id')}",
        "text": text,
        "metadata": {
            "collection": COLLECTION,
            "book_id": book.get("id"),
            "title": book.get("title"),
            "level": book.get("level"),
            "license": book.get("license"),
            "topics": book.get("topics") or [],
            "languages": book.get("languages") or [],
            "url_primary": book.get("url_primary"),
            "url_pdf": book.get("url_pdf"),
            "source": book.get("source"),
            "year": book.get("year"),
        },
    }


def parse_all(catalog: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = catalog or load_catalog()
    docs: list[dict[str, Any]] = []
    for book in data.get("books") or []:
        if not book.get("id") or not book.get("title"):
            continue
        docs.append(book_to_document(book))
    return docs


def index_into_rag_store(store: Any, catalog_path: str | None = None) -> dict[str, Any]:
    """
    store must expose upsert(collection, documents) or similar.
    Compatible with AION rag store patterns used by scenario_index.
    """
    path = resolve_catalog_path(catalog_path) if catalog_path else resolve_catalog_path()
    catalog = load_catalog(path)
    docs = parse_all(catalog)
    upserted = 0
    errors: list[str] = []

    # AION rag store signature:
    #   upsert(collection, text, *, source="", meta=None, chunk_id=None) -> str
    if not hasattr(store, "upsert"):
        raise RuntimeError("RAG store has no upsert method")
    for d in docs:
        try:
            # Use the book id as both source and chunk_id so the same
            # book always maps to the same chunk row.
            store.upsert(
                COLLECTION,
                d["text"],
                source=d["id"],
                meta=d.get("metadata") or {},
                chunk_id=hashlib.sha256(f"{COLLECTION}:{d['id']}".encode()).hexdigest()[:24],
            )
            upserted += 1
        except Exception as e:
            errors.append(f"{d['id']}: {e}")

    return {
        "ok": upserted > 0 and not errors,
        "collection": COLLECTION,
        "catalog_path": str(path),
        "books_in_catalog": len(catalog.get("books") or []),
        "documents": len(docs),
        "upserted": upserted,
        "errors": errors[:20],
    }


# --- skill entrypoints ---

async def coding_books_index(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Skill: coding.books.index — parse catalog and upsert into RAG."""
    try:
        from ..rag import store as rag_store  # type: ignore
    except Exception:
        try:
            from app.skills.rag import store as rag_store  # type: ignore
        except Exception as e:
            return {
                "ok": False,
                "error_code": "rag_store_unavailable",
                "message": f"cannot import rag store: {e}",
            }

    if hasattr(rag_store, "get_rag_store"):
        st = rag_store.get_rag_store()
    elif hasattr(rag_store, "get_store"):
        st = rag_store.get_store()
    else:
        st = rag_store
    try:
        result = index_into_rag_store(st, catalog_path=args.get("catalog_path"))
        result["skill_id"] = "coding.books.index"
        return result
    except FileNotFoundError as e:
        return {"ok": False, "error_code": "catalog_not_found", "message": str(e)}
    except Exception as e:
        logger.exception("coding.books.index failed")
        return {"ok": False, "error_code": "index_failed", "message": str(e)}


async def coding_books_search(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Skill: coding.books.search — query collection coding_books."""
    query = (args.get("query") or args.get("q") or "").strip()
    if not query:
        return {"ok": False, "error_code": "invalid_args", "message": "query required"}
    limit = int(args.get("limit") or 5)
    try:
        from ..rag import store as rag_store  # type: ignore
    except Exception:
        from app.skills.rag import store as rag_store  # type: ignore

    if hasattr(rag_store, "get_rag_store"):
        st = rag_store.get_rag_store()
    elif hasattr(rag_store, "get_store"):
        st = rag_store.get_store()
    else:
        st = rag_store
    if hasattr(st, "search"):
        hits = st.search(COLLECTION, query, limit=limit)
    elif hasattr(st, "query"):
        hits = st.query(COLLECTION, query, k=limit)
    else:
        return {"ok": False, "error_code": "rag_search_unavailable", "message": "store has no search"}
    return {
        "ok": True,
        "skill_id": "coding.books.search",
        "collection": COLLECTION,
        "query": query,
        "count": len(hits) if hits is not None else 0,
        "hits": hits,
    }


async def coding_books_catalog(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Skill: coding.books.catalog — list parsed books (no embeddings)."""
    try:
        catalog = load_catalog(Path(args["catalog_path"]) if args.get("catalog_path") else None)
    except Exception as e:
        return {"ok": False, "error_code": "catalog_not_found", "message": str(e)}
    books = catalog.get("books") or []
    level = (args.get("level") or "").strip().lower()
    topic = (args.get("topic") or "").strip().lower()
    lang = (args.get("language") or "").strip().lower()
    out = []
    for b in books:
        if level and (b.get("level") or "").lower() != level:
            continue
        if topic and topic not in [t.lower() for t in (b.get("topics") or [])]:
            continue
        if lang and lang not in [x.lower() for x in (b.get("languages") or [])]:
            continue
        out.append(
            {
                "id": b.get("id"),
                "title": b.get("title"),
                "level": b.get("level"),
                "topics": b.get("topics"),
                "languages": b.get("languages"),
                "url_primary": b.get("url_primary"),
                "url_pdf": b.get("url_pdf"),
                "license": b.get("license"),
            }
        )
    return {
        "ok": True,
        "skill_id": "coding.books.catalog",
        "count": len(out),
        "books": out,
    }
