"""
AION Audit — append-only decision + call log.
"""
from __future__ import annotations
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .settings import settings


class AuditLog:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path or settings.audit_log_dir) / "audit.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, payload: Dict[str, Any]) -> None:
        entry = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            "event": event,
            **payload,
        }
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:  # audit must never crash the request
            print(f"[AION-AUDIT] failed to write: {exc}")

    def recent(self, n: int = 50) -> list:
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                lines = f.readlines()[-n:]
            return [json.loads(l) for l in lines if l.strip()]
        except Exception:
            return []


audit = AuditLog()
