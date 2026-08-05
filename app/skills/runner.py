"""Skill runner — execute only; never invent results."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .base import SkillError
from .registry_core import SkillSpec, get_registry

ExecutorFn = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class SkillResult:
    ok: bool
    skill_id: str
    run_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "skill_id": self.skill_id,
            "run_id": self.run_id,
            "data": self.data if self.ok else {},
            "error_code": self.error_code,
            "error_message": self.error_message,
            "latency_ms": self.latency_ms,
        }


def _validate(spec: SkillSpec, args: dict[str, Any]) -> str | None:
    schema = spec.input_schema or {}
    for key in schema.get("required") or []:
        if key not in args:
            return f"missing_required:{key}"
    return None


class SkillRunner:
    def __init__(self) -> None:
        self.registry = get_registry()
        self._exec: dict[str, ExecutorFn] = {}

    def register(self, key: str, fn: ExecutorFn) -> None:
        self._exec[key] = fn

    def register_many(self, m: dict[str, ExecutorFn]) -> None:
        self._exec.update(m)

    async def run(
        self,
        skill_id: str,
        args: dict[str, Any] | None = None,
        *,
        subject: str | None = None,
        ctx: dict[str, Any] | None = None,
    ) -> SkillResult:
        args = args or {}
        ctx = dict(ctx or {})
        spec = self.registry.get(skill_id)
        if not spec:
            return SkillResult(False, skill_id, error_code="skill_not_found", error_message=skill_id)
        if not spec.enabled:
            return SkillResult(False, skill_id, error_code="skill_disabled")
        err = _validate(spec, args)
        if err:
            return SkillResult(False, skill_id, error_code="invalid_args", error_message=err)
        key = spec.executor or f"builtin:{spec.id}"
        fn = self._exec.get(key) or self._exec.get(spec.id)
        if not fn:
            rid = self.registry.record_run(skill_id=skill_id, ok=False, subject=subject, error_code="skill_executor_not_wired")
            return SkillResult(False, skill_id, run_id=rid, error_code="skill_executor_not_wired", error_message=key)
        t0 = time.perf_counter()
        try:
            data = await fn(args, ctx)
            if not isinstance(data, dict):
                data = {"value": data}
            ms = int((time.perf_counter() - t0) * 1000)
            rid = self.registry.record_run(
                skill_id=skill_id,
                ok=True,
                subject=subject,
                latency_ms=ms,
                input_hash=hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16],
            )
            return SkillResult(True, skill_id, run_id=rid, data=data, latency_ms=ms)
        except SkillError as e:
            ms = int((time.perf_counter() - t0) * 1000)
            rid = self.registry.record_run(skill_id=skill_id, ok=False, subject=subject, error_code=e.error_code, latency_ms=ms)
            return SkillResult(False, skill_id, run_id=rid, error_code=e.error_code, error_message=str(e), latency_ms=ms)
        except Exception as e:
            ms = int((time.perf_counter() - t0) * 1000)
            rid = self.registry.record_run(skill_id=skill_id, ok=False, subject=subject, error_code="skill_exception", latency_ms=ms)
            return SkillResult(False, skill_id, run_id=rid, error_code="skill_exception", error_message=str(e)[:400], latency_ms=ms)


_runner: SkillRunner | None = None


def get_runner() -> SkillRunner:
    global _runner
    if _runner is None:
        _runner = SkillRunner()
    return _runner
