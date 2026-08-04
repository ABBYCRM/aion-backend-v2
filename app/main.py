"""AION Runtime - full main (env-only settings)."""
import asyncio, json
from typing import List
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .audit import audit
from .kernel import (
    AION_CONTINUITY_PACK, DecisionState, MissionContext,
    build_system_prompt, resolve_decision,
)
from .llm import AllProvidersFailed, probe, stream_chat
from .settings import settings

app = FastAPI(title=f"{settings.app_name} Runtime", version=settings.app_version)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": settings.app_name, "version": settings.app_version}

@app.get("/readyz")
async def readyz():
    providers = await probe()
    return {"ok": any(p.get("ok") for p in providers.values()), "providers": providers}

@app.get("/api/continuity-pack")
async def continuity_pack():
    return AION_CONTINUITY_PACK

@app.get("/api/models")
async def models():
    return {"chain": settings.model_chain, "primary": settings.model_chain[0] if settings.model_chain else None}

class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(system|user|assistant|tool)$")
    content: str = Field(..., min_length=1, max_length=200_000)

class ChatRequest(BaseModel):
    messages: List[ChatMessage] = Field(..., min_length=1, max_length=settings.max_context_messages)
    temperature: float = 0.7
    max_tokens: int = 2048
    stream: bool = True
    metadata: dict = Field(default_factory=dict)

class DecisionRequest(BaseModel):
    user_input: str = Field(..., min_length=1, max_length=20_000)
    history: List[ChatMessage] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)

@app.post("/api/decision")
async def decision(req: DecisionRequest):
    ctx = MissionContext(user_input=req.user_input, history=[m.model_dump() for m in req.history], metadata=req.metadata)
    dec = resolve_decision(ctx)
    audit.record("kernel.decision", {"request_id": ctx.request_id, "state": dec.state.value, "score": dec.score, "fingerprint": ctx.fingerprint()})
    return {"request_id": ctx.request_id, "decision": dec.to_dict(), "system_prompt": build_system_prompt(dec)}

@app.post("/api/chat")
async def chat(req: ChatRequest):
    last_user = next((m for m in reversed(req.messages) if m.role == "user"), None)
    if not last_user:
        raise HTTPException(400, "user message required")
    ctx = MissionContext(user_input=last_user.content, history=[m.model_dump() for m in req.messages[:-1]], metadata=req.metadata)
    decision = resolve_decision(ctx)
    audit.record("kernel.pre_chat", {"request_id": ctx.request_id, "state": decision.state.value, "score": decision.score, "fingerprint": ctx.fingerprint()})
    system_prompt = build_system_prompt(decision)
    model_messages = [{"role": "system", "content": system_prompt}]
    model_messages.extend([m.model_dump() for m in req.messages])
    if len(model_messages) > settings.max_context_messages:
        sys_msg = model_messages[0]
        rest = model_messages[1:]
        rest = rest[-(settings.max_context_messages - 1):]
        model_messages = [sys_msg] + rest
    if decision.state == DecisionState.REJECT:
        async def reject_stream():
            yield f"data: {json.dumps({'type': 'rejected'})}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"
        return StreamingResponse(reject_stream(), media_type="text/event-stream")
    async def event_stream():
        yield f"data: {json.dumps({'type': 'decision', 'decision': decision.to_dict()})}\n\n".encode("utf-8")
        try:
            async for event_type, payload in stream_chat(
                model_chain=settings.model_chain,
                messages=model_messages,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
                request_id=ctx.request_id,
            ):
                yield f"data: {json.dumps({'type': event_type, **payload})}\n\n".encode("utf-8")
                await asyncio.sleep(0)
        except AllProvidersFailed:
            yield f"data: {json.dumps({'type': 'error', 'kind': 'all_providers_failed'})}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"
    return StreamingResponse(event_stream(), media_type="text/event-stream")

@app.on_event("startup")
async def on_startup():
    audit.record("aion.startup", {"version": settings.app_version, "env": settings.environment})

@app.on_event("shutdown")
async def on_shutdown():
    audit.record("aion.shutdown", {})
