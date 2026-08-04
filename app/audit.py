"""Redacted structured audit logging.

Every event is emitted as one JSON line to stdout so DigitalOcean captures it.
When ``DATABASE_URL`` is configured, events are also persisted in PostgreSQL.
Development/test environments may additionally use ``AUDIT_LOG_PATH``.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .settings import settings

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - runtime requirements install psycopg.
    psycopg = None
    dict_row = None
    Jsonb = None

_SENSITIVE_KEYS = {
    "authorization", "api_key", "token", "password", "private_key", "secret",
    "value", "content", "messages", "system_prompt", "prompt", "key",
}


def _redact(value: Any, key: str = "") -> Any:
    lowered = key.lower()
    if any(marker in lowered for marker in _SENSITIVE_KEYS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value[:50]]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "…"
    return value


class AuditLog:
    def __init__(self) -> None:
        self.path = Path(settings.audit_log_path) if settings.audit_log_path else None
        self._lock = threading.Lock()
        self._recent: deque[dict[str, Any]] = deque(maxlen=200)
        self._database_ready = False

    def initialize(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if not settings.database_url:
            return
        if psycopg is None:
            raise RuntimeError("psycopg_not_installed")
        with psycopg.connect(settings.database_url, connect_timeout=min(settings.request_timeout_seconds, 30)) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS aion_audit (
                    id BIGSERIAL PRIMARY KEY,
                    ts DOUBLE PRECISION NOT NULL,
                    iso TEXT NOT NULL,
                    event TEXT NOT NULL,
                    payload JSONB NOT NULL
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_aion_audit_ts ON aion_audit(ts DESC)")
        self._database_ready = True

    def close(self) -> None:
        self._database_ready = False

    def status(self) -> dict[str, Any]:
        return {
            "persistent": self._database_ready,
            "backend": "postgres" if self._database_ready else "stdout",
        }

    def record(self, event: str, payload: dict[str, Any] | None = None) -> None:
        clean_payload = _redact(payload or {})
        entry = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event,
            **clean_payload,
        }
        encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        print(encoded, flush=True)
        with self._lock:
            self._recent.append(entry)
            if self.path:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(encoded + "\n")
                self._truncate_file()
        if self._database_ready:
            self._insert_database(entry)

    def _insert_database(self, entry: dict[str, Any]) -> None:
        assert psycopg is not None and Jsonb is not None
        payload = {key: value for key, value in entry.items() if key not in {"ts", "iso", "event"}}
        try:
            with psycopg.connect(settings.database_url, connect_timeout=min(settings.request_timeout_seconds, 30)) as db:
                db.execute(
                    "INSERT INTO aion_audit(ts, iso, event, payload) VALUES(%s,%s,%s,%s)",
                    (entry["ts"], entry["iso"], entry["event"], Jsonb(payload)),
                )
        except Exception as exc:
            print(json.dumps({"event": "audit.persist_failed", "error_type": type(exc).__name__}), flush=True)

    def _truncate_file(self) -> None:
        assert self.path is not None
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if len(lines) > settings.audit_retention_lines:
            self.path.write_text(
                "\n".join(lines[-settings.audit_retention_lines:]) + "\n",
                encoding="utf-8",
            )

    def recent(self, n: int = 50) -> list[dict[str, Any]]:
        n = max(1, min(n, 200))
        if self._database_ready:
            assert psycopg is not None and dict_row is not None
            with psycopg.connect(
                settings.database_url,
                connect_timeout=min(settings.request_timeout_seconds, 30),
                row_factory=dict_row,
            ) as db:
                rows = db.execute(
                    "SELECT ts, iso, event, payload FROM aion_audit ORDER BY ts DESC LIMIT %s",
                    (n,),
                ).fetchall()
            output = []
            for row in reversed(rows):
                output.append({"ts": row["ts"], "iso": row["iso"], "event": row["event"], **dict(row["payload"] or {})})
            return output
        with self._lock:
            return list(self._recent)[-n:]


audit = AuditLog()
