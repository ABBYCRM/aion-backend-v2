"""Skill runner — execute registry skills with validation; never invent results.

Wire executors from main AION tools in install step. Until wired, builtins that
are not registered in EXECUTORS return skill_executor_not_wired (hard fail).
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .skills_db import SkillRegistry, SkillSpec, get_registry, validate_input


# async (args, ctx) -> dict
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

    def as_tool_context(self) -> str:
        """Evidence block for the model — only real data or explicit failure."""
        if self.ok:
            body = json.dumps(self.data, ensure_ascii=False, indent=2)[:12_000]
            return f'<tool_results skill="{self.skill_id}" untrusted="true">\n{body}\n</tool_results>'
        return (
            f'<tool_error skill="{self.skill_id}" code="{self.error_code or "error"}">\n'
            f"{self.error_message or self.error_code}\n"
            f"</tool_error>"
        )


class SkillRunner:
    def __init__(self, registry: SkillRegistry | None = None):
        self.registry = registry or get_registry()
        self._executors: dict[str, ExecutorFn] = {}

    def register_executor(self, key: str, fn: ExecutorFn) -> None:
        """key matches SkillSpec.executor (e.g. builtin:github.repo)."""
        self._executors[key] = fn

    def register_many(self, mapping: dict[str, ExecutorFn]) -> None:
        for k, fn in mapping.items():
            self.register_executor(k, fn)

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
        if subject:
            ctx.setdefault("subject", subject)

        spec = self.registry.get(skill_id)
        if not spec:
            return SkillResult(
                ok=False,
                skill_id=skill_id,
                error_code="skill_not_found",
                error_message=f"No skill registered: {skill_id}",
            )
        if not spec.enabled:
            return SkillResult(
                ok=False,
                skill_id=skill_id,
                error_code="skill_disabled",
                error_message=f"Skill disabled: {skill_id}",
            )

        ok_val, err = validate_input(spec, args)
        if not ok_val:
            return SkillResult(
                ok=False,
                skill_id=skill_id,
                error_code="invalid_args",
                error_message=err or "invalid_args",
            )

        executor_key = spec.executor or f"builtin:{spec.id}"
        fn = self._executors.get(executor_key) or self._executors.get(spec.id)
        if not fn:
            result = SkillResult(
                ok=False,
                skill_id=skill_id,
                error_code="skill_executor_not_wired",
                error_message=f"Executor not wired: {executor_key}",
            )
            result.run_id = self.registry.record_run(
                skill_id=skill_id,
                ok=False,
                subject=subject,
                error_code=result.error_code,
                latency_ms=0,
                input_hash=_hash_args(args),
            )
            return result

        t0 = time.perf_counter()
        try:
            data = await fn(args, ctx)
            if not isinstance(data, dict):
                data = {"value": data}
            latency = int((time.perf_counter() - t0) * 1000)
            run_id = self.registry.record_run(
                skill_id=skill_id,
                ok=True,
                subject=subject,
                latency_ms=latency,
                input_hash=_hash_args(args),
            )
            return SkillResult(
                ok=True,
                skill_id=skill_id,
                run_id=run_id,
                data=data,
                latency_ms=latency,
            )
        except Exception as e:
            latency = int((time.perf_counter() - t0) * 1000)
            code = getattr(e, "error_code", None) or type(e).__name__
            # Prefer stable tool error strings when present
            msg = str(e)[:500]
            if msg in (spec.error_codes or []) or "_" in msg and msg.islower():
                code = msg
            run_id = self.registry.record_run(
                skill_id=skill_id,
                ok=False,
                subject=subject,
                error_code=str(code)[:120],
                latency_ms=latency,
                input_hash=_hash_args(args),
            )
            return SkillResult(
                ok=False,
                skill_id=skill_id,
                run_id=run_id,
                error_code=str(code)[:120],
                error_message=msg,
                latency_ms=latency,
            )


def _hash_args(args: dict[str, Any]) -> str:
    raw = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# Singleton runner for app wiring
_runner: SkillRunner | None = None


def get_runner() -> SkillRunner:
    global _runner
    if _runner is None:
        _runner = SkillRunner()
    return _runner
