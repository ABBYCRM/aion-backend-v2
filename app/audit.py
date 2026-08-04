"""AION Audit - safe, no pydantic-settings dep."""
import json, time
from pathlib import Path
class AuditLog:
    def __init__(self):
        path = Path("./data/audit")
        try:
            path.mkdir(parents=True, exist_ok=True)
            self.path = path / "audit.jsonl"
        except Exception:
            self.path = Path("/tmp/aion-audit.log")
    def record(self, event, payload):
        entry = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()), "event": event, **payload}
        for p in [self.path, Path("/tmp/aion-audit.log")]:
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                if p != self.path: self.path = p
                return
            except Exception:
                continue
    def recent(self, n=50):
        try:
            if not self.path.exists(): return []
            with self.path.open("r", encoding="utf-8") as f:
                lines = f.readlines()[-n:]
            return [json.loads(l) for l in lines if l.strip()]
        except Exception:
            return []

audit = AuditLog()
