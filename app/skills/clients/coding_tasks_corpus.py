"""
AION coding-task corpus — 5000 production-grade engineering scenarios.

Collection: coding_tasks
Layer: code_corpus (task intake / drills / evaluation — not books, not tool policy)

Skills:
  coding.tasks.index   — upsert JSONL/CSV-derived docs into RAG collection coding_tasks
  coding.tasks.search  — keyword search (RAG or local CSV fallback)
  coding.tasks.get     — fetch one CT-NNNN by id
  coding.tasks.catalog — filter by domain / task_type / context_name
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("aion.skills.coding_tasks_corpus")

COLLECTION = "coding_tasks"
STOP = frozenset(
    "a an the and or to of in on for is are be with as by at from that this it a".split()
)


def _data_roots() -> list[Path]:
    roots: list[Path] = []
    for key in ("AION_DATA_DIR", "AION_TASKS_DIR"):
        v = os.environ.get(key)
        if v:
            roots.append(Path(v))
            roots.append(Path(v) / "tasks")
    roots.extend(
        [
            Path.cwd() / "data",
            Path.cwd() / "data" / "tasks",
            Path("/app/data"),
            Path("/app/data/tasks"),
        ]
    )
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        s = str(r)
        if s not in seen:
            seen.add(s)
            out.append(r)
    return out


def resolve_csv(explicit: str | None = None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise FileNotFoundError(p)
        return p
    # Try every root in priority order, with both layout shapes
    # (csv at root OR csv under root/tasks/).
    for root in _data_roots():
        cand = root / "coding_tasks_5000.csv"
        if cand.is_file():
            return cand
        cand = root / "tasks" / "coding_tasks_5000.csv"
        if cand.is_file():
            return cand
    raise FileNotFoundError(
        "coding_tasks_5000.csv not found under data/ or $AION_DATA_DIR"
    )


def resolve_jsonl(explicit: str | None = None) -> Path:
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
    for root in _data_roots():
        for c in (root / "coding_tasks_5000.jsonl", root / "tasks" / "coding_tasks_5000.jsonl"):
            if c.is_file():
                return c
    raise FileNotFoundError("coding_tasks_5000.jsonl not found")


_CACHE: list[dict[str, Any]] | None = None


def load_rows(csv_path: str | None = None) -> list[dict[str, Any]]:
    global _CACHE
    if _CACHE is not None and csv_path is None:
        return _CACHE
    path = resolve_csv(csv_path)
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if csv_path is None:
        _CACHE = rows
    return rows


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_+#-]+", (s or "").lower()) if len(t) > 2 and t not in STOP}


def local_search(
    query: str,
    *,
    limit: int = 5,
    domain: str | None = None,
    task_type: str | None = None,
    context_name: str | None = None,
) -> list[dict[str, Any]]:
    q = _tokens(query)
    domain = (domain or "").strip().lower() or None
    task_type = (task_type or "").strip().lower() or None
    context_name = (context_name or "").strip().lower() or None
    scored: list[tuple[float, dict[str, Any]]] = []
    for r in load_rows():
        if domain and domain not in (r.get("domain") or "").lower():
            continue
        if task_type and task_type not in (r.get("task_type") or "").lower():
            continue
        if context_name and context_name not in (r.get("context_name") or "").lower():
            continue
        blob = _tokens(
            " ".join(
                [
                    r.get("domain") or "",
                    r.get("task_type") or "",
                    r.get("system") or "",
                    r.get("title") or "",
                    r.get("objective") or "",
                    r.get("principal_risks") or "",
                    r.get("edge_cases") or "",
                ]
            )
        )
        overlap = len(q & blob)
        if q and overlap == 0:
            continue
        score = float(overlap)
        scored.append((score, r))
    scored.sort(key=lambda x: (-x[0], int(x[1].get("scenario_num") or 0)))
    out = []
    for score, r in scored[: max(1, min(limit, 50))]:
        out.append(
            {
                "id": r.get("id"),
                "score": score,
                "domain": r.get("domain"),
                "task_type": r.get("task_type"),
                "system": r.get("system"),
                "title": r.get("title"),
                "objective": r.get("objective"),
                "context_name": r.get("context_name"),
                "principal_risks": r.get("principal_risks"),
                "edge_cases": r.get("edge_cases"),
                "required_validation": r.get("required_validation"),
            }
        )
    return out


def index_into_rag(store: Any, jsonl_path: str | None = None) -> dict[str, Any]:
    path = resolve_jsonl(jsonl_path)
    upserted = 0
    errors: list[str] = []
    if not hasattr(store, "upsert"):
        raise RuntimeError("RAG store has no upsert")
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            doc = json.loads(line)
            try:
                store.upsert(
                    COLLECTION,
                    doc["text"],
                    source=doc.get("id") or "",
                    meta=doc.get("metadata") or {},
                    chunk_id=doc.get("chunk_id"),
                )
                upserted += 1
            except TypeError:
                try:
                    store.upsert(
                        COLLECTION,
                        doc.get("id"),
                        doc["text"],
                        metadata=doc.get("metadata"),
                    )
                    upserted += 1
                except Exception as e:
                    errors.append(f"{doc.get('id')}: {e}")
            except Exception as e:
                errors.append(f"{doc.get('id')}: {e}")
                if len(errors) > 30:
                    break
    return {
        "ok": upserted > 0 and len(errors) == 0,
        "collection": COLLECTION,
        "upserted": upserted,
        "errors": errors[:20],
        "jsonl_path": str(path),
    }


# --- skill entrypoints ---

async def coding_tasks_index(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    try:
        from app.skills.rag import store as rag_store  # type: ignore
    except Exception as e:
        return {"ok": False, "error_code": "rag_store_unavailable", "message": str(e)}
    st = rag_store.get_rag_store() if hasattr(rag_store, "get_rag_store") else (
        rag_store.get_store() if hasattr(rag_store, "get_store") else rag_store
    )
    try:
        result = index_into_rag(st, args.get("jsonl_path"))
        result["skill_id"] = "coding.tasks.index"
        return result
    except Exception as e:
        logger.exception("coding.tasks.index failed")
        return {"ok": False, "error_code": "index_failed", "message": str(e)}


async def coding_tasks_search(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or args.get("q") or "").strip()
    if not query:
        return {"ok": False, "error_code": "invalid_args", "message": "query required"}
    limit = int(args.get("limit") or 5)
    # Prefer local CSV (deterministic, no embed dependency); optional RAG later
    try:
        hits = local_search(
            query,
            limit=limit,
            domain=args.get("domain"),
            task_type=args.get("task_type"),
            context_name=args.get("context_name"),
        )
        return {
            "ok": True,
            "skill_id": "coding.tasks.search",
            "collection": COLLECTION,
            "query": query,
            "count": len(hits),
            "hits": hits,
            "source": "csv_local",
        }
    except Exception as e:
        return {"ok": False, "error_code": "search_failed", "message": str(e)}


async def coding_tasks_get(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    tid = (args.get("id") or args.get("task_id") or "").strip().upper()
    if not tid:
        return {"ok": False, "error_code": "invalid_args", "message": "id required e.g. CT-0001"}
    if tid.isdigit():
        tid = f"CT-{int(tid):04d}"
    if not tid.startswith("CT-"):
        tid = f"CT-{tid}"
    for r in load_rows():
        if (r.get("id") or "").upper() == tid:
            return {"ok": True, "skill_id": "coding.tasks.get", "task": r}
    return {"ok": False, "error_code": "not_found", "message": f"no task {tid}"}


async def coding_tasks_catalog(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    domain = (args.get("domain") or "").strip().lower()
    task_type = (args.get("task_type") or "").strip().lower()
    context_name = (args.get("context_name") or "").strip().lower()
    limit = int(args.get("limit") or 50)
    out = []
    for r in load_rows():
        if domain and domain not in (r.get("domain") or "").lower():
            continue
        if task_type and task_type not in (r.get("task_type") or "").lower():
            continue
        if context_name and context_name not in (r.get("context_name") or "").lower():
            continue
        out.append(
            {
                "id": r.get("id"),
                "domain": r.get("domain"),
                "task_type": r.get("task_type"),
                "system": r.get("system"),
                "title": r.get("title"),
                "context_name": r.get("context_name"),
            }
        )
        if len(out) >= limit:
            break
    return {
        "ok": True,
        "skill_id": "coding.tasks.catalog",
        "count": len(out),
        "tasks": out,
    }
