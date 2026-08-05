"""FastAPI routes for the skill registry.

Mount from main.py:

    from app.skills_routes import build_router
    app.include_router(build_router(authenticated=authenticated))

Auth: uses the same Depends(authenticated) pattern — inject your principal.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from .skills_db import SkillSpec, get_registry
from .skills_runner import get_runner
from .skills_seed import seed_registry


# ── Request models (module scope so Pydantic v2 can resolve ForwardRef) ──

class RunBody(BaseModel):
    skill_id: str
    args: dict[str, Any] = Field(default_factory=dict)


class UpsertBody(BaseModel):
    id: str
    name: str
    description: str = ""
    version: str = "1.0.0"
    side_effect: str = "read"
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = 30_000
    enabled: bool = True
    executor: str = ""
    tags: list[str] = Field(default_factory=list)
    error_codes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


# Placeholder auth — replace when mounting
async def _optional_auth() -> Any:
    return type("P", (), {"subject": "anonymous"})()


def build_router(authenticated: Callable = _optional_auth) -> APIRouter:
    router = APIRouter(prefix="/api/skills", tags=["skills"])

    @router.get("")
    async def list_skills(
        enabled_only: bool = True,
        tag: Optional[str] = None,
        principal=Depends(authenticated),
    ):
        reg = get_registry()
        return {
            "ok": True,
            "skills": reg.catalog(enabled_only=enabled_only, tag=tag),
            "count": len(reg.list(enabled_only=enabled_only, tag=tag)),
        }

    @router.get("/{skill_id}")
    async def get_skill(skill_id: str, principal=Depends(authenticated)):
        spec = get_registry().get(skill_id)
        if not spec:
            raise HTTPException(status_code=404, detail="skill_not_found")
        return {"ok": True, "skill": spec.public_dict()}

    @router.post("/run")
    async def run_skill(body: RunBody, principal=Depends(authenticated)):
        subject = getattr(principal, "subject", None)
        result = await get_runner().run(body.skill_id, body.args, subject=subject)
        # 404 only for missing skill
        if result.error_code == "skill_not_found":
            raise HTTPException(status_code=404, detail=result.error_code)
        return result.to_dict()

    @router.post("/seed")
    async def seed(principal=Depends(authenticated)):
        n = seed_registry()
        return {"ok": True, "seeded": n}

    @router.post("/upsert")
    async def upsert_skill(body: UpsertBody, principal=Depends(authenticated)):
        spec = SkillSpec(
            id=body.id,
            name=body.name,
            description=body.description,
            version=body.version,
            side_effect=body.side_effect,
            input_schema=body.input_schema,
            output_schema=body.output_schema,
            timeout_ms=body.timeout_ms,
            enabled=body.enabled,
            executor=body.executor,
            tags=body.tags,
            error_codes=body.error_codes,
            metadata=body.metadata,
        )
        try:
            saved = get_registry().upsert(spec)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"ok": True, "skill": saved.public_dict()}

    return router


# Default router with placeholder auth (swap in main)
router = build_router()
