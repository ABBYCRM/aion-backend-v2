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
from .scratchpad import scratchpad, SECRET_KINDS
from .kernel import (
    AION_CONTINUITY_PACK,
    DecisionState,
    MissionContext,
    build_system_prompt,
    resolve_decision,
)
from .llm import AllProvidersFailed, list_models, probe, stream_chat
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
    # Optional model override — user picks from /api/models/all.
    # If present, replaces the default chain. Falls back to chain on failure.
    model: Optional[str] = None
    provider: Optional[str] = None


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


@app.get("/api/models/all")
async def models_all() -> dict:
    """Full picker: every model id from every healthy provider, grouped."""
    providers = await list_models()
    # Build a flat list of (provider, model) for the picker UI.
    flat: List[dict] = []
    for provider_name, info in providers.items():
        if not info.get("ok"):
            continue
        for mid in info.get("models", []):
            flat.append({"provider": provider_name, "model": mid})
    return {
        "providers": providers,
        "flat": flat,
        "chain": settings.model_chain,
        "primary": settings.model_chain[0] if settings.model_chain else None,
    }


@app.get("/api/audit/recent")
async def audit_recent(n: int = 50) -> dict:
    return {"events": audit.recent(n)}


# ---------------------------------------------------------------------------
# Scratchpad — operator-scoped persistent memory.
# Stores API keys, env snippets, project notes. Secrets are masked in list
# responses unless ?reveal=true.
# ---------------------------------------------------------------------------
class ScratchpadAdd(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    value: str = Field(..., min_length=1, max_length=20_000)
    kind: str = Field(default="note", max_length=40)
    tags: List[str] = Field(default_factory=list)
    source: str = Field(default="manual", max_length=60)
    notes: str = Field(default="", max_length=2000)


class ScratchpadUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    value: Optional[str] = Field(default=None, max_length=20_000)
    kind: Optional[str] = Field(default=None, max_length=40)
    tags: Optional[List[str]] = None
    source: Optional[str] = Field(default=None, max_length=60)
    notes: Optional[str] = Field(default=None, max_length=2000)


class ScratchpadDetect(BaseModel):
    text: str = Field(..., min_length=1, max_length=200_000)


@app.get("/api/scratchpad/stats")
async def scratchpad_stats() -> dict:
    return scratchpad.stats()


@app.get("/api/scratchpad")
async def scratchpad_list(
    kind: Optional[str] = None,
    tag: Optional[str] = None,
    q: Optional[str] = None,
    reveal: bool = False,
) -> dict:
    items = scratchpad.list(kind=kind, tag=tag, q=q, reveal=reveal)
    if reveal:
        # Log every reveal of secrets
        for it in items:
            if it.get("kind") in SECRET_KINDS and it.get("revealed"):
                audit.record("scratchpad.reveal", {"id": it["id"], "name": it["name"], "kind": it["kind"]})
    return {"items": items, "count": len(items)}


@app.get("/api/scratchpad/search")
async def scratchpad_search(q: str, reveal: bool = False) -> dict:
    items = scratchpad.list(q=q, reveal=reveal)
    if reveal:
        for it in items:
            if it.get("kind") in SECRET_KINDS and it.get("revealed"):
                audit.record("scratchpad.reveal", {"id": it["id"], "name": it["name"], "kind": it["kind"], "via": "search"})
    return {"items": items, "count": len(items), "q": q}


@app.get("/api/scratchpad/{entry_id}")
async def scratchpad_get(entry_id: str, reveal: bool = False) -> dict:
    e = scratchpad.get(entry_id, reveal=reveal)
    if e is None:
        raise HTTPException(404, "scratchpad entry not found")
    if reveal and e.get("kind") in SECRET_KINDS and e.get("revealed"):
        audit.record("scratchpad.reveal", {"id": e["id"], "name": e["name"], "kind": e["kind"]})
    return e


@app.post("/api/scratchpad")
async def scratchpad_add(body: ScratchpadAdd) -> dict:
    e = scratchpad.add(
        name=body.name,
        value=body.value,
        kind=body.kind,
        tags=body.tags,
        source=body.source,
        notes=body.notes,
    )
    audit.record("scratchpad.add", {"id": e["id"], "name": e["name"], "kind": e["kind"], "tags": e["tags"]})
    # Always return masked unless caller passed reveal. Default to masked.
    view = dict(e)
    if e["kind"] in SECRET_KINDS:
        # Mask the value in the create response too — caller already has it.
        from .scratchpad import _mask
        view["value"] = _mask(e["value"])
        view["revealed"] = False
    return view


@app.patch("/api/scratchpad/{entry_id}")
async def scratchpad_update(entry_id: str, body: ScratchpadUpdate) -> dict:
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if not patch:
        raise HTTPException(400, "no fields to update")
    e = scratchpad.update(entry_id, patch)
    if e is None:
        raise HTTPException(404, "scratchpad entry not found")
    audit.record("scratchpad.update", {"id": e["id"], "fields": list(patch.keys())})
    return e


@app.delete("/api/scratchpad/{entry_id}")
async def scratchpad_delete(entry_id: str) -> dict:
    ok = scratchpad.delete(entry_id)
    if not ok:
        raise HTTPException(404, "scratchpad entry not found")
    audit.record("scratchpad.delete", {"id": entry_id})
    return {"ok": True, "id": entry_id}


@app.post("/api/scratchpad/{entry_id}/use")
async def scratchpad_use(entry_id: str) -> dict:
    e = scratchpad.use(entry_id)
    if e is None:
        raise HTTPException(404, "scratchpad entry not found")
    audit.record("scratchpad.use", {"id": e["id"], "use_count": e["use_count"]})
    return e


@app.post("/api/scratchpad/detect")
async def scratchpad_detect(body: ScratchpadDetect) -> dict:
    """Scan arbitrary text for likely API keys / tokens. Returns masked previews.
    The full plaintext is returned in `value` so the client can offer to save."""
    candidates = scratchpad.detect_in_text(body.text)
    return {"candidates": candidates, "count": len(candidates)}


@app.get("/api/scratchpad/verify")
async def scratchpad_verify_all() -> dict:
    """Re-verify the kernel signature on every entry. Returns a summary."""
    return scratchpad.verify_all()


@app.get("/api/scratchpad/continuity-context")
async def scratchpad_continuity_context(
    q: Optional[str] = None,
    limit: int = 6,
    kind: Optional[str] = None,
) -> dict:
    """Return a formatted text block the LLM can consume as scratchpad context.
    Used by the chat to inject recent/relevant operator memory into the system
    prompt under the 7-law kernel."""
    block = scratchpad.continuity_context(query=q or "", limit=limit, kinds=[kind] if kind else None)
    return {"context": block, "length": len(block)}


@app.get("/api/scratchpad/lawset")
async def scratchpad_lawset() -> dict:
    """Return the embedded lawset version + 7 law names so the operator can
    verify which kernel signed an entry."""
    from .scratchpad import LAWSET_VERSION, LAW_NAMES
    return {
        "lawset_version": LAWSET_VERSION,
        "laws": LAW_NAMES,
    }


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
    # Inject the operator's scratchpad as continuity context. This is how
    # the LLM "remembers" API keys, env vars, and project notes across
    # sessions without the operator having to paste them in every chat.
    sp_ctx = scratchpad.continuity_context(
        query=last_user.content,
        limit=6,
    )
    if sp_ctx:
        system_prompt = system_prompt + "\n\n" + sp_ctx
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
        # Build the model chain. If the user picked a specific model
        # in the picker, override the chain with just that one — plus
        # the rest of the chain as a fallback in case the picked model
        # is unavailable.
        if req.model:
            # User picked something specific. Try it first, then fall
            # through to the rest of the chain.
            picked = [req.model]
            rest = [m for m in settings.model_chain if m != req.model]
            chain = picked + rest
        else:
            chain = list(settings.model_chain)

        try:
            async for event_type, payload in stream_chat(
                model_chain=chain,
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
