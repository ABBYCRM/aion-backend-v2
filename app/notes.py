"""Owner-scoped notes with durable PostgreSQL support.

Production on DigitalOcean App Platform requires ``DATABASE_URL``. Local SQLite
is available only for development and tests. Notes are never a secret store and
are excluded from model context unless the client explicitly opts in.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from .settings import settings

try:  # Optional at import time so source-only validation still works.
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - CI installs the runtime dependency.
    psycopg = None
    dict_row = None
    Jsonb = None

_ALLOWED_KINDS = {"note", "project", "url"}
_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bnvapi-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?:API[_ -]?KEY|TOKEN|PASSWORD|SECRET|PRIVATE[_ -]?KEY|CREDENTIAL)\s*[:=]", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.I),
]
_STOP_WORDS = {
    "about", "after", "again", "also", "and", "are", "can", "could", "from",
    "have", "into", "just", "more", "please", "that", "the", "their", "then",
    "this", "what", "when", "where", "which", "with", "would", "your",
}


class SecretLikeValue(ValueError):
    """Raised when note content resembles a credential."""


class NotesUnavailable(RuntimeError):
    """Raised when production notes have no durable database configured."""


class NotesStore:
    def __init__(self) -> None:
        self.backend = settings.notes_backend
        self.path = Path(settings.notes_db_path) if self.backend == "sqlite" else None
        self._initialized = False

    @property
    def available(self) -> bool:
        return self.backend in {"sqlite", "postgres"} and self._initialized

    @property
    def persistent(self) -> bool:
        return self.backend == "postgres" and self._initialized

    def status(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "persistent": self.persistent,
            "backend": self.backend,
        }

    def initialize(self) -> None:
        if self.backend == "disabled":
            self._initialized = False
            return
        if self.backend == "postgres":
            self._initialize_postgres()
        else:
            self._initialize_sqlite()
        self._initialized = True

    def close(self) -> None:
        self._initialized = False

    def _ensure_available(self) -> None:
        if not self.available:
            raise NotesUnavailable("notes_require_database_url_in_production")

    def _sqlite_connect(self) -> sqlite3.Connection:
        assert self.path is not None
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _postgres_connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("psycopg_not_installed")
        return psycopg.connect(
            settings.database_url,
            connect_timeout=min(settings.request_timeout_seconds, 30),
            row_factory=dict_row,
        )

    def _initialize_sqlite(self) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._sqlite_connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    value TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '[]',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_notes_owner_updated ON notes(owner, updated_at DESC)")

    def _initialize_postgres(self) -> None:
        with self._postgres_connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS aion_notes (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    value TEXT NOT NULL,
                    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
                    created_at BIGINT NOT NULL,
                    updated_at BIGINT NOT NULL
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_aion_notes_owner_updated "
                "ON aion_notes(owner, updated_at DESC)"
            )

    @staticmethod
    def _validate(name: str, kind: str, value: str, tags: list[str]) -> tuple[str, str, str, list[str]]:
        name = name.strip()
        kind = kind.strip().lower()
        value = value.strip()
        normalized_tags = sorted({tag.strip().lower() for tag in tags if tag.strip()})
        if not name or len(name) > 200:
            raise ValueError("invalid_note_name")
        if kind not in _ALLOWED_KINDS:
            raise ValueError("invalid_note_kind")
        if not value or len(value) > 20_000:
            raise ValueError("invalid_note_value")
        combined = "\n".join((name, value, *normalized_tags))
        if any(pattern.search(combined) for pattern in _SECRET_PATTERNS):
            raise SecretLikeValue("credentials_must_use_deployment_secrets")
        if len(normalized_tags) > 20 or any(len(tag) > 50 for tag in normalized_tags):
            raise ValueError("invalid_note_tags")
        return name, kind, value, normalized_tags

    def list(self, owner: str, *, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
        self._ensure_available()
        limit = max(1, min(limit, 100))
        query = query.strip()[:200]
        if self.backend == "postgres":
            sql = "SELECT * FROM aion_notes WHERE owner = %s"
            params: list[Any] = [owner]
            if query:
                sql += " AND (name ILIKE %s OR value ILIKE %s OR tags::text ILIKE %s)"
                like = f"%{query}%"
                params.extend([like, like, like])
            sql += " ORDER BY updated_at DESC LIMIT %s"
            params.append(limit)
            with self._postgres_connect() as db:
                rows = db.execute(sql, params).fetchall()
        else:
            sql = "SELECT * FROM notes WHERE owner = ?"
            params = [owner]
            if query:
                sql += " AND (name LIKE ? OR value LIKE ? OR tags LIKE ?)"
                like = f"%{query}%"
                params.extend([like, like, like])
            sql += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            with self._sqlite_connect() as db:
                rows = db.execute(sql, params).fetchall()
        return [self._row(row) for row in rows]

    def add(self, owner: str, *, name: str, kind: str, value: str, tags: list[str]) -> dict[str, Any]:
        self._ensure_available()
        name, kind, value, normalized_tags = self._validate(name, kind, value, tags)
        now = int(time.time())
        note_id = f"note_{uuid.uuid4().hex[:16]}"
        if self.backend == "postgres":
            assert Jsonb is not None
            with self._postgres_connect() as db:
                row = db.execute(
                    """
                    INSERT INTO aion_notes(id, owner, name, kind, value, tags, created_at, updated_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING *
                    """,
                    (note_id, owner, name, kind, value, Jsonb(normalized_tags), now, now),
                ).fetchone()
        else:
            with self._sqlite_connect() as db:
                db.execute(
                    "INSERT INTO notes(id, owner, name, kind, value, tags, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (note_id, owner, name, kind, value, json.dumps(normalized_tags), now, now),
                )
                row = db.execute(
                    "SELECT * FROM notes WHERE id = ? AND owner = ?", (note_id, owner)
                ).fetchone()
        return self._row(row)

    def update(
        self,
        owner: str,
        note_id: str,
        *,
        name: str,
        kind: str,
        value: str,
        tags: list[str],
    ) -> dict[str, Any] | None:
        self._ensure_available()
        name, kind, value, normalized_tags = self._validate(name, kind, value, tags)
        now = int(time.time())
        if self.backend == "postgres":
            assert Jsonb is not None
            with self._postgres_connect() as db:
                row = db.execute(
                    """
                    UPDATE aion_notes
                    SET name=%s, kind=%s, value=%s, tags=%s, updated_at=%s
                    WHERE id=%s AND owner=%s
                    RETURNING *
                    """,
                    (name, kind, value, Jsonb(normalized_tags), now, note_id, owner),
                ).fetchone()
        else:
            with self._sqlite_connect() as db:
                cursor = db.execute(
                    "UPDATE notes SET name=?, kind=?, value=?, tags=?, updated_at=? "
                    "WHERE id=? AND owner=?",
                    (name, kind, value, json.dumps(normalized_tags), now, note_id, owner),
                )
                if cursor.rowcount == 0:
                    return None
                row = db.execute(
                    "SELECT * FROM notes WHERE id = ? AND owner = ?", (note_id, owner)
                ).fetchone()
        return self._row(row) if row else None

    def delete(self, owner: str, note_id: str) -> bool:
        self._ensure_available()
        if self.backend == "postgres":
            with self._postgres_connect() as db:
                cursor = db.execute(
                    "DELETE FROM aion_notes WHERE id = %s AND owner = %s", (note_id, owner)
                )
        else:
            with self._sqlite_connect() as db:
                cursor = db.execute(
                    "DELETE FROM notes WHERE id = ? AND owner = ?", (note_id, owner)
                )
        return cursor.rowcount > 0

    def context(self, owner: str, query: str, limit: int = 5) -> str:
        """Return bounded, explicitly untrusted JSONL context for matching notes only."""
        self._ensure_available()
        terms = self._search_terms(query)
        if not terms:
            return ""
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for term in terms:
            for item in self.list(owner, query=term, limit=limit):
                if item["id"] in seen or item["kind"] not in _ALLOWED_KINDS:
                    continue
                seen.add(item["id"])
                selected.append(item)
                if len(selected) >= limit:
                    break
            if len(selected) >= limit:
                break
        if not selected:
            return ""
        lines = ["<operator_notes format=\"jsonl\" untrusted=\"true\">"]
        used = len(lines[0])
        for item in selected:
            safe_item = {
                "name": item["name"],
                "kind": item["kind"],
                "tags": item["tags"],
                "value": item["value"][:2_000],
            }
            encoded = self._safe_json(safe_item)
            if used + len(encoded) + 32 > settings.max_notes_context_chars:
                break
            lines.append(encoded)
            used += len(encoded) + 1
        lines.append("</operator_notes>")
        return "\n".join(lines) if len(lines) > 2 else ""

    @staticmethod
    def _search_terms(query: str) -> list[str]:
        words = re.findall(r"[A-Za-z0-9_.:/-]{3,}", query.lower())
        output: list[str] = []
        for word in words:
            if word in _STOP_WORDS or word in output:
                continue
            output.append(word[:80])
            if len(output) >= 5:
                break
        return output

    @staticmethod
    def _safe_json(value: Any) -> str:
        return (
            json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
        )

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        tags = row["tags"]
        if isinstance(tags, str):
            try:
                parsed = json.loads(tags)
                tags = parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                tags = [tag for tag in tags.split(",") if tag]
        return {
            "id": row["id"],
            "name": row["name"],
            "kind": row["kind"],
            "value": row["value"],
            "tags": list(tags or []),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


notes = NotesStore()
