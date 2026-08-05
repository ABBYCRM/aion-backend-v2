"""Shared skill types and errors — no secrets."""
from __future__ import annotations

from typing import Any


class SkillError(Exception):
    def __init__(self, code: str, message: str | None = None):
        self.error_code = code
        super().__init__(message or code)


def require_env(*names: str) -> dict[str, str]:
    import os
    out: dict[str, str] = {}
    missing = []
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if not v:
            missing.append(n)
        else:
            out[n] = v
    if missing:
        raise SkillError("skill_not_configured", f"missing_env:{','.join(missing)}")
    return out


def env_any(*names: str) -> tuple[str, str] | None:
    """Return (name, value) for first set env var."""
    import os
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v:
            return n, v
    return None
