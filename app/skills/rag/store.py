"""Local SQLite vector-ish RAG store (chunk text + optional embedding blob).

Works offline without Pinecone. If PINECONE_API_KEY + index are set, pinecone.py
can be used as upgrade path; this store is always available for skills RAG.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


def _db_path() -> Path:
    base = os.environ.get("AION_DATA_DIR") or "./data"
    Path(base).mkdir(parents=True, exist_ok=True)
    return Path(base) / "rag_skills.db"


class LocalRagStore:
    def __init__(self, path: str | Path | None = None):
        self.path = str(path or _db_path())
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
              id TEXT PRIMARY KEY,
              collection TEXT NOT NULL,
              source TEXT,
              text TEXT NOT NULL,
              meta TEXT,
              created_at REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_coll ON chunks(collection)"
        )
        self._conn.commit()

    def upsert(
        self,
        collection: str,
        text: str,
        *,
        source: str = "",
        meta: dict[str, Any] | None = None,
        chunk_id: str | None = None,
    ) -> str:
        text = (text or "").strip()
        if not text:
            raise ValueError("empty_text")
        cid = chunk_id or hashlib.sha256(f"{collection}:{source}:{text[:200]}".encode()).hexdigest()[:24]
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO chunks(id, collection, source, text, meta, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  text=excluded.text, meta=excluded.meta, source=excluded.source
                """,
                (cid, collection, source, text, json.dumps(meta or {}), time.time()),
            )
            self._conn.commit()
        return cid

    def search(self, collection: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Keyword ranking (no embedding required). Deterministic, no hallucination."""
        q = (query or "").lower().strip()
        tokens = [t for t in re.split(r"\W+", q) if len(t) > 2]
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM chunks WHERE collection = ? ORDER BY created_at DESC LIMIT 500",
                (collection,),
            ).fetchall()
        scored: list[tuple[float, dict]] = []
        for r in rows:
            text = r["text"] or ""
            low = text.lower()
            score = 0.0
            if q and q in low:
                score += 5.0
            for t in tokens:
                score += low.count(t) * 1.0
            if score > 0:
                scored.append(
                    (
                        score,
                        {
                            "id": r["id"],
                            "source": r["source"],
                            "text": text[:4000],
                            "score": score,
                            "meta": json.loads(r["meta"] or "{}"),
                        },
                    )
                )
        scored.sort(key=lambda x: -x[0])
        return [x[1] for x in scored[: max(1, min(limit, 20))]]

    def count(self, collection: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM chunks WHERE collection = ?", (collection,)
            ).fetchone()
        return int(row["n"] if row else 0)


_store: LocalRagStore | None = None


def get_rag_store() -> LocalRagStore:
    global _store
    if _store is None:
        _store = LocalRagStore()
    return _store
