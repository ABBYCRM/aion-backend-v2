"""SQLite skill registry for main AION.

Skills are micro-software: fixed id, JSON schemas, side-effect class, timeout,
and an optional executor path. The model must not invent skill results — the
registry is the source of truth for what can run.

DB file: $AION_DATA_DIR/skills.db (default ./data/skills.db)
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA_VERSION = 1

# Side-effect classes — runner + policy use these
SIDE_EFFECTS = frozenset({"none", "read", "write", "network", "admin"})


@dataclass
class SkillSpec:
    """Canonical skill contract."""

    id: str
    name: str
    description: str
    version: str = "1.0.0"
    side_effect: str = "read"  # none|read|write|network|admin
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    timeout_ms: int = 30_000
    enabled: bool = True
    executor: str = ""  # module:function or builtin key
    tags: list[str] = field(default_factory=list)
    error_codes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    def validate_self(self) -> None:
        if not self.id or not self.id.replace(".", "").replace("_", "").replace("-", "").isalnum():
            # allow dotted ids like github.get_repo
            if not self.id or any(c for c in self.id if not (c.isalnum() or c in "._-")):
                raise ValueError("invalid_skill_id")
        if self.side_effect not in SIDE_EFFECTS:
            raise ValueError(f"invalid_side_effect:{self.side_effect}")
        if self.timeout_ms < 100 or self.timeout_ms > 600_000:
            raise ValueError("timeout_ms_out_of_range")
        if not isinstance(self.input_schema, dict) or not isinstance(self.output_schema, dict):
            raise ValueError("schemas_must_be_objects")

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "side_effect": self.side_effect,
            "input_schema": json.dumps(self.input_schema, ensure_ascii=False),
            "output_schema": json.dumps(self.output_schema, ensure_ascii=False),
            "timeout_ms": int(self.timeout_ms),
            "enabled": 1 if self.enabled else 0,
            "executor": self.executor or "",
            "tags": json.dumps(list(self.tags), ensure_ascii=False),
            "error_codes": json.dumps(list(self.error_codes), ensure_ascii=False),
            "metadata": json.dumps(self.metadata or {}, ensure_ascii=False),
            "created_at": self.created_at or time.time(),
            "updated_at": self.updated_at or time.time(),
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict[str, Any]) -> "SkillSpec":
        r = dict(row)
        return cls(
            id=r["id"],
            name=r["name"],
            description=r["description"] or "",
            version=r.get("version") or "1.0.0",
            side_effect=r.get("side_effect") or "read",
            input_schema=_json_obj(r.get("input_schema")),
            output_schema=_json_obj(r.get("output_schema")),
            timeout_ms=int(r.get("timeout_ms") or 30_000),
            enabled=bool(r.get("enabled", 1)),
            executor=r.get("executor") or "",
            tags=_json_list(r.get("tags")),
            error_codes=_json_list(r.get("error_codes")),
            metadata=_json_obj(r.get("metadata")),
            created_at=float(r.get("created_at") or 0),
            updated_at=float(r.get("updated_at") or 0),
        )

    def public_dict(self) -> dict[str, Any]:
        """Catalog view for the model / API (no secrets)."""
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


def _json_obj(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    try:
        out = json.loads(value)
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


def _json_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    try:
        out = json.loads(value)
        return out if isinstance(out, list) else []
    except Exception:
        return []


def _default_db_path() -> Path:
    base = os.environ.get("AION_DATA_DIR") or os.environ.get("LLM_GATEWAY_DATA_DIR") or "./data"
    Path(base).mkdir(parents=True, exist_ok=True)
    return Path(base) / "skills.db"


class SkillRegistry:
    """Thread-safe SQLite registry of skills."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = str(db_path or _default_db_path())
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
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
                CREATE INDEX IF NOT EXISTS idx_skills_enabled ON skills(enabled);
                CREATE INDEX IF NOT EXISTS idx_skills_side_effect ON skills(side_effect);

                CREATE TABLE IF NOT EXISTS skill_runs (
                  run_id TEXT PRIMARY KEY,
                  skill_id TEXT NOT NULL,
                  subject TEXT,
                  ok INTEGER NOT NULL,
                  error_code TEXT,
                  latency_ms INTEGER,
                  input_hash TEXT,
                  created_at REAL NOT NULL,
                  FOREIGN KEY (skill_id) REFERENCES skills(id)
                );
                CREATE INDEX IF NOT EXISTS idx_skill_runs_skill ON skill_runs(skill_id, created_at);
                """
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── registry CRUD ─────────────────────────────────────────────

    def upsert(self, spec: SkillSpec) -> SkillSpec:
        spec.validate_self()
        now = time.time()
        existing = self.get(spec.id)
        if existing:
            spec.created_at = existing.created_at or now
        else:
            spec.created_at = spec.created_at or now
        spec.updated_at = now
        row = spec.to_row()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO skills (
                  id, name, description, version, side_effect,
                  input_schema, output_schema, timeout_ms, enabled, executor,
                  tags, error_codes, metadata, created_at, updated_at
                ) VALUES (
                  :id, :name, :description, :version, :side_effect,
                  :input_schema, :output_schema, :timeout_ms, :enabled, :executor,
                  :tags, :error_codes, :metadata, :created_at, :updated_at
                )
                ON CONFLICT(id) DO UPDATE SET
                  name=excluded.name,
                  description=excluded.description,
                  version=excluded.version,
                  side_effect=excluded.side_effect,
                  input_schema=excluded.input_schema,
                  output_schema=excluded.output_schema,
                  timeout_ms=excluded.timeout_ms,
                  enabled=excluded.enabled,
                  executor=excluded.executor,
                  tags=excluded.tags,
                  error_codes=excluded.error_codes,
                  metadata=excluded.metadata,
                  updated_at=excluded.updated_at
                """,
                row,
            )
            self._conn.commit()
        return self.get(spec.id)  # type: ignore

    def get(self, skill_id: str) -> SkillSpec | None:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,))
            row = cur.fetchone()
        return SkillSpec.from_row(row) if row else None

    def delete(self, skill_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM skills WHERE id = ?", (skill_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def list(
        self,
        *,
        enabled_only: bool = True,
        tag: str | None = None,
        side_effect: str | None = None,
    ) -> list[SkillSpec]:
        q = "SELECT * FROM skills WHERE 1=1"
        args: list[Any] = []
        if enabled_only:
            q += " AND enabled = 1"
        if side_effect:
            q += " AND side_effect = ?"
            args.append(side_effect)
        q += " ORDER BY id ASC"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        skills = [SkillSpec.from_row(r) for r in rows]
        if tag:
            skills = [s for s in skills if tag in (s.tags or [])]
        return skills

    def catalog(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Model-facing list (public_dict only)."""
        return [s.public_dict() for s in self.list(**kwargs)]

    def set_enabled(self, skill_id: str, enabled: bool) -> SkillSpec | None:
        with self._lock:
            self._conn.execute(
                "UPDATE skills SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, time.time(), skill_id),
            )
            self._conn.commit()
        return self.get(skill_id)

    def record_run(
        self,
        *,
        skill_id: str,
        ok: bool,
        subject: str | None = None,
        error_code: str | None = None,
        latency_ms: int | None = None,
        input_hash: str | None = None,
    ) -> str:
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO skill_runs(
                  run_id, skill_id, subject, ok, error_code, latency_ms, input_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    skill_id,
                    subject,
                    1 if ok else 0,
                    error_code,
                    latency_ms,
                    input_hash,
                    time.time(),
                ),
            )
            self._conn.commit()
        return run_id

    def seed(self, specs: Iterable[SkillSpec]) -> int:
        n = 0
        for spec in specs:
            self.upsert(spec)
            n += 1
        return n


# ── minimal required-field check (no external jsonschema dependency) ─

def validate_input(spec: SkillSpec, args: dict[str, Any]) -> tuple[bool, str | None]:
    """Lightweight validation against input_schema.required and types if present."""
    if not isinstance(args, dict):
        return False, "args_must_be_object"
    schema = spec.input_schema or {}
    required = schema.get("required") or []
    props = schema.get("properties") or {}
    for key in required:
        if key not in args:
            return False, f"missing_required:{key}"
    for key, val in args.items():
        if key not in props:
            # allow additional unless schema says false
            if schema.get("additionalProperties") is False:
                return False, f"unknown_property:{key}"
            continue
        prop = props[key] or {}
        expected = prop.get("type")
        if not expected:
            continue
        if not _type_ok(val, expected):
            return False, f"type_mismatch:{key}:{expected}"
    return True, None


def _type_ok(val: Any, expected: str | list) -> bool:
    types = expected if isinstance(expected, list) else [expected]
    for t in types:
        if t == "string" and isinstance(val, str):
            return True
        if t == "number" and isinstance(val, (int, float)) and not isinstance(val, bool):
            return True
        if t == "integer" and isinstance(val, int) and not isinstance(val, bool):
            return True
        if t == "boolean" and isinstance(val, bool):
            return True
        if t == "object" and isinstance(val, dict):
            return True
        if t == "array" and isinstance(val, list):
            return True
        if t == "null" and val is None:
            return True
    return False


_registry_singleton: SkillRegistry | None = None
_registry_lock = threading.Lock()


def get_registry(db_path: str | Path | None = None) -> SkillRegistry:
    global _registry_singleton
    with _registry_lock:
        if _registry_singleton is None:
            _registry_singleton = SkillRegistry(db_path=db_path)
        return _registry_singleton
