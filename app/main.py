"""
AION Runtime — FastAPI server.
"""
from __future__ import annotations
import asyncio
import json
import time
from typing import AsyncIterator, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
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


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title=f"{settings.app_name} Runtime",
    version=settings.app_version,
    description="Adaptive Intelligence Operating Nexus — Megalithic Intelligence Lattice",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(system|user|assistant|tool)$")
    content: str = Field(..., min_length=1, max_length=200_000)


class ChatRequest(BaseModel):
    messages: List[ChatMessage] = Field(..., min_length=1, max_length=settings.max_context_messages)
    temperature: float = 0.7
    max_tokens: int = 2048
    stream: bool = True
    metadata: dict = Field(default_factory=dict)


class HealthResponse(BaseModel):
    ok: bool
    service: str
    version: str
    environment: str
    providers: dict
    timestamp: str
    continuity_pack_id: str


class DecisionRequest(BaseModel):
    user_input: str = Field(..., min_length=1, max_length=20_000)
    history: List[ChatMessage] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "service": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
    }


@app.get("/readyz")
async def readyz() -> HealthResponse:
    providers = await probe()
    ok = any(p.get("ok") for p in providers.values())
    if not ok:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "service": settings.app_name,
                "version": settings.app_version,
                "environment": settings.environment,
                "providers": providers,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
                "continuity_pack_id": AION_CONTINUITY_PACK["system_name"],
                "error": "no_working_provider",
            },
        )
    return HealthResponse(
        ok=True,
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        providers=providers,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime()),
        continuity_pack_id=AION_CONTINUITY_PACK["system_name"],
    )


@app.get("/api/continuity-pack")
async def continuity_pack() -> dict:
    return AION_CONTINUITY_PACK


@app.get("/api/models")
async def models() -> dict:
    chain = settings.model_chain
    providers = await probe()
    return {
        "chain": chain,
        "providers": providers,
        "primary": chain[0] if chain else None,
    }


@app.get("/api/audit/recent")
async def audit_recent(n: int = 50) -> dict:
    return {"events": audit.recent(n)}


@app.post("/api/decision")
async def decision(req: DecisionRequest) -> dict:
    ctx = MissionContext(
        user_input=req.user_input,
        history=[m.model_dump() for m in req.history],
        metadata=req.metadata,
    )
    dec = resolve_decision(ctx)
    audit.record("kernel.decision", {
        "request_id": ctx.request_id,
        "state": dec.state.value,
        "score": dec.score,
        "fingerprint": ctx.fingerprint(),
    })
    return {
        "request_id": ctx.request_id,
        "decision": dec.to_dict(),
        "system_prompt": build_system_prompt(dec),
    }


@app.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    """Server-Sent Events streaming chat endpoint."""
    if not req.messages:
        raise HTTPException(400, "messages must be non-empty")

    last_user = next((m for m in reversed(req.messages) if m.role == "user"), None)
    if not last_user:
        raise HTTPException(400, "at least one user message is required")

    # Run the kernel on the latest user turn
    ctx = MissionContext(
        user_input=last_user.content,
        history=[m.model_dump() for m in req.messages[:-1]],
        metadata=req.metadata,
    )
    decision = resolve_decision(ctx)

    audit.record("kernel.pre_chat", {
        "request_id": ctx.request_id,
        "state": decision.state.value,
        "score": decision.score,
        "fingerprint": ctx.fingerprint(),
    })

    # Build the message stack sent to the model
    system_prompt = build_system_prompt(decision)
    model_messages = [{"role": "system", "content": system_prompt}]
    model_messages.extend([m.model_dump() for m in req.messages])
    # Truncate to max_context_messages
    if len(model_messages) > settings.max_context_messages:
        # always keep system, drop oldest in the middle
        sys_msg = model_messages[0]
        rest = model_messages[1:]
        rest = rest[-(settings.max_context_messages - 1):]
        model_messages = [sys_msg] + rest

    if decision.state == DecisionState.REJECT:
        async def reject_stream() -> AsyncIterator[bytes]:
            payload = {
                "type": "rejected",
                "request_id": ctx.request_id,
                "decision": decision.to_dict(),
                "message": "The kernel rejected this turn. Adjust your request and try again.",
            }
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"
        return StreamingResponse(reject_stream(), media_type="text/event-stream")

    async def event_stream() -> AsyncIterator[bytes]:
        # 1. Send the decision so the client can show it
        yield f"data: {json.dumps({'type': 'decision', 'decision': decision.to_dict(), 'request_id': ctx.request_id}, ensure_ascii=False)}\n\n".encode("utf-8")

        # 2. Stream from the LLM router
        try:
            async for event_type, payload in stream_chat(
                model_chain=settings.model_chain,
                messages=model_messages,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
                request_id=ctx.request_id,
            ):
                msg = {"type": event_type, **payload}
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n".encode("utf-8")
                await asyncio.sleep(0)  # cooperative yield
        except AllProvidersFailed as e:
            err = {"type": "error", "kind": "all_providers_failed", "message": str(e)}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode("utf-8")
            audit.record("llm.all_failed", {"request_id": ctx.request_id, "error": str(e)})

        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-AION-Decision": decision.state.value,
        },
    )


# ---------------------------------------------------------------------------
# TTS proxy — browser SpeechSynthesis is the primary path; this is a
# server-side fallback that streams an audio file if a TTS key is configured.
# We expose the endpoint so the frontend can call it when available.
# ---------------------------------------------------------------------------
@app.post("/api/tts")
async def tts(body: dict) -> JSONResponse:
    text = (body or {}).get("text", "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    # Server-side TTS is intentionally not implemented in v1.1 —
    # we use the browser's Web Speech API on the client (zero key, zero cost).
    return JSONResponse({
        "ok": True,
        "mode": "client",
        "text": text,
        "note": "Client-side Web Speech API is used. No server TTS key required.",
    })


# ---------------------------------------------------------------------------
# Static frontend mount (served from same origin in production)
# ---------------------------------------------------------------------------
import os as _os
_STATIC_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "static")
if _os.path.isdir(_STATIC_DIR):
    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")


# ---------------------------------------------------------------------------
# Startup probe
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def on_startup() -> None:
    audit.record("aion.startup", {"version": settings.app_version, "env": settings.environment})
    # Run the live provider probe in the background with a hard timeout so
    # it never blocks startup. If a provider is slow/unreachable, the server
    # still comes up and serves /healthz; the probe result lands in the audit log.
    async def _bg_probe() -> None:
        try:
            providers = await asyncio.wait_for(probe(), timeout=20.0)
            audit.record("aion.providers_probe", providers)
            for p, info in providers.items():
                print(f"[AION] provider={p} ok={info.get('ok')} models={info.get('model_count', '?')}")
        except asyncio.TimeoutError:
            print("[AION] startup probe timed out after 20s")
            audit.record("aion.providers_probe_failed", {"error": "timeout"})
        except Exception as e:
            print(f"[AION] startup probe failed: {e}")
            audit.record("aion.providers_probe_failed", {"error": str(e)})
    asyncio.create_task(_bg_probe())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    audit.record("aion.shutdown", {})
