"""FastAPI router for skills — mount on main AION."""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .registry_core import get_registry
from .runner import get_runner
from .seed_all import bootstrap


async def _anon():
    return type("P", (), {"subject": "anonymous"})()


class RunBody(BaseModel):
    skill_id: str
    args: dict[str, Any] = Field(default_factory=dict)


def build_router(authenticated: Callable = _anon) -> APIRouter:
    router = APIRouter(prefix="/api/skills", tags=["skills"])

    @router.get("")
    async def list_skills(enabled_only: bool = True, tag: Optional[str] = None, principal=Depends(authenticated)):
        reg = get_registry()
        items = reg.catalog(enabled_only=enabled_only, tag=tag)
        return {"ok": True, "count": len(items), "skills": items}

    @router.post("/run")
    async def run_skill(body: RunBody, principal=Depends(authenticated)):
        result = await get_runner().run(body.skill_id, body.args, subject=getattr(principal, "subject", None))
        if result.error_code == "skill_not_found":
            raise HTTPException(status_code=404, detail="skill_not_found")
        return result.to_dict()

    @router.post("/bootstrap")
    async def do_bootstrap(principal=Depends(authenticated)):
        return {"ok": True, **bootstrap()}

    return router
