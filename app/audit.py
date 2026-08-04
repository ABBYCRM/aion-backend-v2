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
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            # Audit must never break startup. Fall back to /tmp.
            print(f"[AION-AUDIT] init mkdir failed ({exc}); using /tmp/aion-audit.log")
            self.path = Path("/tmp/aion-audit.log")

    def record(self, event: str, payload: Dict[str, Any]) -> None:
        entry = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
            "event": event,
            **payload,
        }
        # Try primary path; on any failure, fall back to /tmp.
        for p in [self.path, Path("/tmp/aion-audit.log")]:
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                if p != self.path:
                    self.path = p  # adopt the working path
                return
            except Exception as exc:
                continue
        print(f"[AION-AUDIT] all write attempts failed for event={event}")

    def recent(self, n: int = 50) -> list:
        try:
            if not self.path.exists():
                return []
            with self.path.open("r", encoding="utf-8") as f:
                lines = f.readlines()[-n:]
            return [json.loads(l) for l in lines if l.strip()]
        except Exception:
            return []


audit = AuditLog()
