"""
AION Runtime — FastAPI server (minimal diagnostic).
"""
from __future__ import annotations
import asyncio
import json
import time
from typing import AsyncIterator, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .audit import audit
from .kernel import (
    AION_CONTINUITY_PACK,
    DecisionState,
    MissionContext,
    build_system_prompt,
    resolve_decision,
)
from .llm import AllProvidersFailed, probe, stream_chat
from .settings import settings

app = FastAPI(
    title=f"{settings.app_name} Runtime",
    version=settings.app_version,
    description="Adaptive Intelligence Operating Nexus",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": settings.app_name, "version": settings.app_version}


@app.get("/readyz")
async def readyz() -> dict:
    providers = await probe()
    return {"ok": any(p.get("ok") for p in providers.values()), "providers": providers}
