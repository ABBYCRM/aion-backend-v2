"""Redacted structured audit logging."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .settings import settings

_SENSITIVE_KEYS = {"authorization", "api_key", "token", "password", "private_key", "secret", "value", "content", "messages", "system_prompt", "prompt"}


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
        self.path = Path(settings.audit_log_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, event: str, payload: dict[str, Any] | None = None) -> None:
        entry = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **_redact(payload or {})}
        encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(encoded + "\n")
            self._truncate_if_needed()

    def _truncate_if_needed(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if len(lines) > settings.audit_retention_lines:
            self.path.write_text("\n".join(lines[-settings.audit_retention_lines:]) + "\n", encoding="utf-8")

    def recent(self, n: int = 50) -> list[dict[str, Any]]:
        n = max(1, min(n, 200))
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        except OSError:
            return []
        output = []
        for line in lines:
            try:
                output.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return output


audit = AuditLog()
