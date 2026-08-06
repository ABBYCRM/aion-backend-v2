"""Skill registry core — SQLite. Copied/adapted for full pack self-containment."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SIDE_EFFECTS = frozenset({"none", "read", "write", "network", "admin"})


@dataclass
class SkillSpec:
    id: str
    name: str
    description: str
    version: str = "1.0.0"
    side_effect: str = "read"
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    timeout_ms: int = 30_000
    enabled: bool = True
    executor: str = ""
    tags: list[str] = field(default_factory=list)
    error_codes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    def validate_self(self) -> None:
        if not self.id or any(c for c in self.id if not (c.isalnum() or c in "._-")):
            raise ValueError("invalid_skill_id")
        if self.side_effect not in SIDE_EFFECTS:
            raise ValueError(f"invalid_side_effect:{self.side_effect}")

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "side_effect": self.side_effect,
            "input_schema": json.dumps(self.input_schema),
            "output_schema": json.dumps(self.output_schema),
            "timeout_ms": int(self.timeout_ms),
            "enabled": 1 if self.enabled else 0,
            "executor": self.executor or "",
            "tags": json.dumps(self.tags),
            "error_codes": json.dumps(self.error_codes),
            "metadata": json.dumps(self.metadata or {}),
            "created_at": self.created_at or time.time(),
            "updated_at": self.updated_at or time.time(),
        }

    @classmethod
    def from_row(cls, row: Any) -> "SkillSpec":
        r = dict(row)

        def jo(v, default):
            if v is None or v == "":
                return default
            if isinstance(v, (dict, list)):
                return v
            try:
                return json.loads(v)
            except Exception:
                return default

        return cls(
            id=r["id"],
            name=r["name"],
            description=r.get("description") or "",
            version=r.get("version") or "1.0.0",
            side_effect=r.get("side_effect") or "read",
            input_schema=jo(r.get("input_schema"), {}),
            output_schema=jo(r.get("output_schema"), {}),
            timeout_ms=int(r.get("timeout_ms") or 30000),
            enabled=bool(r.get("enabled", 1)),
            executor=r.get("executor") or "",
            tags=jo(r.get("tags"), []),
            error_codes=jo(r.get("error_codes"), []),
            metadata=jo(r.get("metadata"), {}),
            created_at=float(r.get("created_at") or 0),
            updated_at=float(r.get("updated_at") or 0),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "side_effect": self.side_effect,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "timeout_ms": self.timeout_ms,
            "enabled": self.enabled,
            "tags": self.tags,
            "error_codes": self.error_codes,
        }


class SkillRegistry:
    def __init__(self, db_path: str | Path | None = None):
        base = os.environ.get("AION_DATA_DIR") or "./data"
        Path(base).mkdir(parents=True, exist_ok=True)
        self.db_path = str(db_path or Path(base) / "skills.db")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL mode is preferred for concurrent readers/writers, but it
        # requires a filesystem that supports shared memory mappings
        # (some FUSE, NFS, and read-only sandbox volumes reject it
        # with "disk I/O error"). Fall back to DELETE mode silently
        # rather than crashing on first startup.
        try:
            self._conn.execute("PRAGMA journal_mode=WAL;")
        except sqlite3.OperationalError:
            self._conn.execute("PRAGMA journal_mode=DELETE;")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS skills (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              version TEXT NOT NULL DEFAULT '1.0.0',
              side_effect TEXT NOT NULL DEFAULT 'read',
              input_schema TEXT NOT NULL DEFAULT '{}',
              output_schema TEXT NOT NULL DEFAULT '{}',
              timeout_ms INTEGER NOT NULL DEFAULT 30000,
              enabled INTEGER NOT NULL DEFAULT 1,
              executor TEXT NOT NULL DEFAULT '',
              tags TEXT NOT NULL DEFAULT '[]',
              error_codes TEXT NOT NULL DEFAULT '[]',
              metadata TEXT NOT NULL DEFAULT '{}',
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS skill_runs (
              run_id TEXT PRIMARY KEY,
              skill_id TEXT NOT NULL,
              subject TEXT,
              ok INTEGER NOT NULL,
              error_code TEXT,
              latency_ms INTEGER,
              input_hash TEXT,
              created_at REAL NOT NULL
            );
            """
        )
        self._conn.commit()

    def upsert(self, spec: SkillSpec) -> SkillSpec:
        spec.validate_self()
        now = time.time()
        existing = self.get(spec.id)
        spec.created_at = (existing.created_at if existing else now)
        spec.updated_at = now
        row = spec.to_row()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO skills (
                  id, name, description, version, side_effect, input_schema, output_schema,
                  timeout_ms, enabled, executor, tags, error_codes, metadata, created_at, updated_at
                ) VALUES (
                  :id, :name, :description, :version, :side_effect, :input_schema, :output_schema,
                  :timeout_ms, :enabled, :executor, :tags, :error_codes, :metadata, :created_at, :updated_at
                )
                ON CONFLICT(id) DO UPDATE SET
                  name=excluded.name, description=excluded.description, version=excluded.version,
                  side_effect=excluded.side_effect, input_schema=excluded.input_schema,
                  output_schema=excluded.output_schema, timeout_ms=excluded.timeout_ms,
                  enabled=excluded.enabled, executor=excluded.executor, tags=excluded.tags,
                  error_codes=excluded.error_codes, metadata=excluded.metadata, updated_at=excluded.updated_at
                """,
                row,
            )
            self._conn.commit()
        return self.get(spec.id)  # type: ignore

    def get(self, skill_id: str) -> SkillSpec | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
        return SkillSpec.from_row(row) if row else None

    def list(self, *, enabled_only: bool = True, tag: str | None = None) -> list[SkillSpec]:
        q = "SELECT * FROM skills"
        if enabled_only:
            q += " WHERE enabled = 1"
        q += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(q).fetchall()
        skills = [SkillSpec.from_row(r) for r in rows]
        if tag:
            skills = [s for s in skills if tag in (s.tags or [])]
        return skills

    def catalog(self, **kw: Any) -> list[dict[str, Any]]:
        return [s.public_dict() for s in self.list(**kw)]

    def seed(self, specs: Iterable[SkillSpec]) -> int:
        n = 0
        for s in specs:
            self.upsert(s)
            n += 1
        return n

    def record_run(self, **kw: Any) -> str:
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO skill_runs(run_id, skill_id, subject, ok, error_code, latency_ms, input_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    kw.get("skill_id"),
                    kw.get("subject"),
                    1 if kw.get("ok") else 0,
                    kw.get("error_code"),
                    kw.get("latency_ms"),
                    kw.get("input_hash"),
                    time.time(),
                ),
            )
            self._conn.commit()
        return run_id


_reg: SkillRegistry | None = None


def get_registry() -> SkillRegistry:
    global _reg
    if _reg is None:
        _reg = SkillRegistry()
    return _reg
