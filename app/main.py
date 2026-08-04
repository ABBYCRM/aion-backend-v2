"""
AION Runtime — FastAPI server (incremental build).
"""
from __future__ import annotations
import asyncio
import json
from typing import List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .audit import audit
from .kernel import AION_CONTINUITY_PACK, resolve_decision, build_system_prompt
from .llm import probe, stream_chat, AllProvidersFailed
from .settings import settings
from .kernel import MissionContext, DecisionState

app = FastAPI(title=f"{settings.app_name} Runtime", version=settings.app_version)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": settings.app_name, "version": settings.app_version}


@app.get("/readyz")
async def readyz() -> dict:
    return {"ok": True, "service": settings.app_name, "version": settings.app_version}


@app.get("/api/continuity-pack")
async def continuity_pack() -> dict:
    return AION_CONTINUITY_PACK


@app.get("/api/models")
async def models() -> dict:
    return {"chain": settings.model_chain, "primary": settings.model_chain[0] if settings.model_chain else None}


@app.on_event("startup")
async def on_startup():
    audit.record("aion.startup", {"version": settings.app_version})
    async def _bg():
        try:
            providers = await asyncio.wait_for(probe(), timeout=20.0)
            for p, info in providers.items():
                print(f"[AION] provider={p} ok={info.get('ok')}")
        except Exception as e:
            print(f"[AION] probe failed: {e}")
    asyncio.create_task(_bg())
