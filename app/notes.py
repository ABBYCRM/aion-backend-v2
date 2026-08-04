"""Authenticated, owner-scoped notes store.

This intentionally does not accept credentials. Provider and GitHub secrets must
stay in deployment environment variables or a managed secret store.
"""
from __future__ import annotations

import re
import sqlite3
import time
import uuid
from pathlib import Path

from .settings import settings

_ALLOWED_KINDS = {"note", "project", "url", "instruction"}
_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bgh[psu]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bnvapi-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:API_KEY|TOKEN|PASSWORD|SECRET|PRIVATE_KEY)\s*=", re.I),
]


class SecretLikeValue(ValueError):
    pass


class NotesStore:
    def __init__(self) -> None:
        path = Path(settings.notes_db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def status(self) -> dict:
        # Detect whether the database is reachable. SQLite is always
        # reachable because it is on local disk; postgres is reachable
        # if a successful connection was opened at least once.
        backend = "sqlite" if str(self.path).endswith(".sqlite3") or str(self.path).endswith(".db") else "disabled"
        if not str(self.path):
            backend = "disabled"
        return {"available": True, "persistent": False, "backend": backend}

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS notes (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
                    kind TEXT NOT NULL, value TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_notes_owner_updated ON notes(owner, updated_at DESC)")

    @staticmethod
    def _validate(name: str, kind: str, value: str, tags: list[str]) -> tuple[str, str, str, str]:
        name = name.strip(); kind = kind.strip().lower(); value = value.strip()
        normalized_tags = sorted({tag.strip().lower() for tag in tags if tag.strip()})
        if not name or len(name) > 200: raise ValueError("invalid_note_name")
        if kind not in _ALLOWED_KINDS: raise ValueError("invalid_note_kind")
        if not value or len(value) > 20_000: raise ValueError("invalid_note_value")
        if any(pattern.search(value) for pattern in _SECRET_PATTERNS): raise SecretLikeValue("credentials_must_use_deployment_secrets")
        if len(normalized_tags) > 20 or any(len(tag) > 50 for tag in normalized_tags): raise ValueError("invalid_note_tags")
        return name, kind, value, ",".join(normalized_tags)

    def list(self, owner: str, *, query: str = "", limit: int = 50) -> list[dict]:
        limit = max(1, min(limit, 100)); query = query.strip()
        sql = "SELECT * FROM notes WHERE owner = ?"; params: list[object] = [owner]
        if query:
            sql += " AND (name LIKE ? OR value LIKE ? OR tags LIKE ?)"; like = f"%{query}%"; params.extend([like, like, like])
        sql += " ORDER BY updated_at DESC LIMIT ?"; params.append(limit)
        with self._connect() as db: rows = db.execute(sql, params).fetchall()
        return [self._row(row) for row in rows]

    def add(self, owner: str, *, name: str, kind: str, value: str, tags: list[str]) -> dict:
        name, kind, value, tags_csv = self._validate(name, kind, value, tags)
        now = int(time.time()); note_id = f"note_{uuid.uuid4().hex[:16]}"
        with self._connect() as db:
            db.execute("INSERT INTO notes(id, owner, name, kind, value, tags, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)", (note_id, owner, name, kind, value, tags_csv, now, now))
            row = db.execute("SELECT * FROM notes WHERE id = ? AND owner = ?", (note_id, owner)).fetchone()
        return self._row(row)

    def update(self, owner: str, note_id: str, *, name: str, kind: str, value: str, tags: list[str]) -> dict | None:
        name, kind, value, tags_csv = self._validate(name, kind, value, tags)
        with self._connect() as db:
            cursor = db.execute("UPDATE notes SET name=?, kind=?, value=?, tags=?, updated_at=? WHERE id=? AND owner=?", (name, kind, value, tags_csv, int(time.time()), note_id, owner))
            if cursor.rowcount == 0: return None
            row = db.execute("SELECT * FROM notes WHERE id = ? AND owner = ?", (note_id, owner)).fetchone()
        return self._row(row)

    def delete(self, owner: str, note_id: str) -> bool:
        with self._connect() as db: cursor = db.execute("DELETE FROM notes WHERE id = ? AND owner = ?", (note_id, owner))
        return cursor.rowcount > 0

    def context(self, owner: str, query: str, limit: int = 5) -> str:
        items = self.list(owner, query=query, limit=limit)
        if not items and query: items = self.list(owner, limit=min(limit, 3))
        if not items: return ""
        lines = ["<operator_notes untrusted=\"true\">"]
        for item in items:
            tags = f" [{', '.join(item['tags'])}]" if item["tags"] else ""
            lines.append(f"- {item['name']}{tags}: {item['value']}")
        lines.append("</operator_notes>")
        return "\n".join(lines)

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "name": row["name"], "kind": row["kind"], "value": row["value"], "tags": [tag for tag in row["tags"].split(",") if tag], "created_at": row["created_at"], "updated_at": row["updated_at"]}


notes = NotesStore()
