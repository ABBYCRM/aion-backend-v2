"""AION FastAPI application."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from .audit import audit
from .auth import Principal, require_admin, require_principal
from .kernel import AION_CONTINUITY_PACK, MissionContext, build_system_prompt, resolve_decision
from .llm import AllProvidersFailed, InvalidModelSelection, configured_providers, list_models, probe, resolve_model_chain, stream_chat
from .notes import SecretLikeValue, notes
from .rate_limit import _ChatCapacityExhausted, enforce_rate_limit, limiter
from .settings import settings
from .tools import ToolConfigurationError, ToolRequestError, github, web_search
from .vault import KNOWN_KEYS as VAULT_KNOWN_KEYS, VaultError, VaultNotConfigured, _fingerprint, ping_all, vault
from . import brain_client
from .gallery import gallery

_GITHUB_URL = re.compile(
    r"https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)",
    re.I,
)

class TextPart(BaseModel):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=settings.max_message_chars)

class ImageURL(BaseModel):
    url: str = Field(min_length=32, max_length=1_500_000)
    @field_validator("url")
    @classmethod
    def validate_data_image(cls, value: str) -> str:
        allowed = ("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,", "data:image/gif;base64,")
        if not value.startswith(allowed): raise ValueError("only_base64_image_data_urls_are_allowed")
        return value

class ImagePart(BaseModel):
    type: Literal["image_url"]
    image_url: ImageURL

ContentPart = Annotated[TextPart | ImagePart, Field(discriminator="type")]

class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str | list[ContentPart]
    @field_validator("content")
    @classmethod
    def validate_content(cls, value):
        if isinstance(value, str):
            if not value.strip() or len(value) > settings.max_message_chars: raise ValueError("invalid_message_content")
            return value
        if not value or len(value) > 12: raise ValueError("invalid_content_parts")
        if sum(len(part.text) for part in value if isinstance(part, TextPart)) > settings.max_message_chars: raise ValueError("message_text_too_large")
        return value
    def wire_content(self): return self.content if isinstance(self.content, str) else [part.model_dump() for part in self.content]
    def text_content(self): return self.content if isinstance(self.content, str) else "\n".join(part.text for part in self.content if isinstance(part, TextPart)).strip()

class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=settings.max_context_messages)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=settings.min_completion_tokens, le=settings.max_completion_tokens)
    model: str | None = Field(default=None, min_length=1, max_length=300)
    provider: str | None = Field(default=None, min_length=1, max_length=40)
    web_search: bool = False
    github_repository: str | None = Field(default=None, max_length=200)
    github_path: str | None = Field(default=None, max_length=1000)
    github_query: str | None = Field(default=None, max_length=200)
    use_notes: bool = True
    @model_validator(mode="after")
    def pair(self):
        if bool(self.model) != bool(self.provider): raise ValueError("provider_and_model_are_required_together")
        return self

class DecisionRequest(BaseModel):
    user_input: str = Field(min_length=1, max_length=20000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=100)
class NoteBody(BaseModel):
    name: str = Field(min_length=1, max_length=200); kind: Literal["note", "project", "url", "instruction"] = "note"; value: str = Field(min_length=1, max_length=20000); tags: list[str] = Field(default_factory=list, max_length=20)
class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=400); count: int = Field(default=6, ge=1, le=20); freshness: Literal["pd", "pw", "pm", "py"] | None = None
class GitHubRepoBody(BaseModel): repository: str = Field(min_length=3, max_length=200)
class GitHubFileBody(GitHubRepoBody): path: str = Field(min_length=1, max_length=1000); ref: str | None = Field(default=None, max_length=200)
class GitHubSearchBody(GitHubRepoBody): query: str = Field(min_length=1, max_length=200); limit: int = Field(default=10, ge=1, le=30)
class GitHubIssueWrite(GitHubRepoBody): title: str = Field(min_length=1, max_length=256); body: str = Field(default="", max_length=60000)
class GitHubBranchWrite(GitHubRepoBody): branch: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._/-]+$"); base: str = Field(default="main", min_length=1, max_length=200)
class GitHubFileWrite(GitHubRepoBody): path: str = Field(min_length=1, max_length=1000); content: str = Field(max_length=500000); message: str = Field(min_length=1, max_length=250); branch: str = Field(min_length=1, max_length=200)
class GitHubPullWrite(GitHubRepoBody): title: str = Field(min_length=1, max_length=256); body: str = Field(default="", max_length=60000); head: str = Field(min_length=1, max_length=200); base: str = Field(default="main", min_length=1, max_length=200)

class BodyLimitMiddleware:
    def __init__(self, app: Any, max_bytes: int): self.app = app; self.max_bytes = max_bytes
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http": return await self.app(scope, receive, send)
        headers = {key.lower(): value for key, value in scope.get("headers", [])}; length = headers.get(b"content-length")
        if length:
            try:
                if int(length) > self.max_bytes: return await JSONResponse(status_code=413, content={"detail": "request_too_large"})(scope, receive, send)
            except ValueError: pass
        received = 0
        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes: raise HTTPException(status_code=413, detail="request_too_large")
            return message
        try: await self.app(scope, limited_receive, send)
        except HTTPException as exc: await JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})(scope, receive, send)

@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.validate_startup()
    audit.record("aion.startup", {"version": settings.app_version, "environment": settings.environment})
    try:
        seeded = vault.reconcile_with_env()
        if seeded: audit.record("vault.reconciled", {"seeded": seeded, "total": len(VAULT_KNOWN_KEYS)})
    except VaultNotConfigured as exc:
        audit.record("vault.disabled", {"reason": str(exc)})
    # Verify Brain connectivity on startup (logs only, never crashes unless AION_BRAIN_REQUIRED=true)
    try:
        brain_client.require_or_warn()
        if brain_client.is_configured():
            await brain_client.verify_on_startup()
    except Exception as exc:
        audit.record("brain.boot.failed", {"error": str(exc)[:200]})
        if settings.brain_required:
            raise
    yield
    audit.record("aion.shutdown", {})

app = FastAPI(title=f"{settings.app_name} Runtime", version=settings.app_version, description="Authenticated AION runtime with web and GitHub tools", docs_url=None if settings.environment == "production" else "/docs", redoc_url=None, lifespan=lifespan)
app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_request_bytes)
app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], allow_headers=["Authorization", "Content-Type", "X-AION-Key", "X-AION-Confirm"], expose_headers=["X-AION-Brain", "X-AION-Brain-Latency-Ms", "X-AION-Brain-Decision", "X-Request-Id"], max_age=600)

@app.exception_handler(ToolConfigurationError)
async def tool_config(_: Request, exc: ToolConfigurationError): return JSONResponse(status_code=200, content={"ok": False, "error": str(exc), "kind": "tool_not_configured"})
@app.exception_handler(ToolRequestError)
async def tool_request(_: Request, exc: ToolRequestError): return JSONResponse(status_code=400, content={"detail": str(exc)})
@app.exception_handler(_ChatCapacityExhausted)
async def chat_capacity(_: Request, exc: _ChatCapacityExhausted): return JSONResponse(status_code=200, content={"ok": False, "error": "chat_capacity_exhausted", "kind": "rate_limited", "retry_after_seconds": 1})

async def authenticated(request: Request, principal: Principal = Depends(require_principal)):
    await enforce_rate_limit(request, principal); return principal
async def confirmed_admin(principal: Principal = Depends(require_admin), confirmation: str | None = Header(default=None, alias="X-AION-Confirm")):
    if (confirmation or "").strip().lower() != "yes": raise HTTPException(status_code=409, detail="explicit_confirmation_required")
    return principal

@app.get("/healthz")
async def healthz(): return {"ok": True, "service": settings.app_name, "version": settings.app_version, "timestamp": int(time.time())}
@app.get("/readyz")
async def readyz():
    providers = configured_providers(); return JSONResponse(status_code=200 if providers else 503, content={"ok": bool(providers), "service": settings.app_name, "version": settings.app_version, "configured_provider_count": len(providers)})
@app.get("/api/continuity-pack")
async def continuity_pack(_: Principal = Depends(authenticated)): return AION_CONTINUITY_PACK
@app.get("/api/models")
async def models(_: Principal = Depends(authenticated)): return {"chain": settings.model_chain, "primary": settings.primary_model, "providers": await probe()}
@app.get("/api/models/all")
async def models_all(_: Principal = Depends(authenticated)):
    providers = await list_models(refresh=False); flat = [{"provider": provider, "model": model} for provider, info in providers.items() if info.get("ok") for model in info.get("models", [])]
    return {"providers": providers, "flat": flat, "chain": settings.model_chain, "primary": settings.primary_model}
@app.get("/api/audit/recent")
async def audit_recent(n: int = Query(default=50, ge=1, le=200), _: Principal = Depends(require_admin)): return {"events": audit.recent(n)}


# ===========================================================================
# Brain-link visual signal — AION <-> Aion-Brain topbar + per-message badge
# ===========================================================================

@app.get("/api/brain/status")
async def brain_status(_: Principal = Depends(authenticated)):
    """Batched health probe for the UI topbar. Never raises."""
    brain = await brain_client.probe_brain()
    return {"ok": True, "brain": brain}


@app.post("/api/brain/probe")
async def brain_probe(_: Principal = Depends(require_admin)):
    """Admin-only: force a fresh probe + audit-log the result."""
    brain = await brain_client.probe_brain()
    audit.record("brain.probe", brain)
    return {"ok": True, "brain": brain}

@app.get("/api/notes/status")
async def notes_status(_: Principal = Depends(authenticated)):
    return notes.status()

@app.get("/api/notes")
async def list_notes(q: str = Query(default="", max_length=200), limit: int = Query(default=50, ge=1, le=100), principal: Principal = Depends(authenticated)):
    items = notes.list(principal.subject, query=q, limit=limit); return {"items": items, "count": len(items)}
@app.post("/api/notes")
async def add_note(body: NoteBody, principal: Principal = Depends(authenticated)):
    try: item = notes.add(principal.subject, **body.model_dump())
    except SecretLikeValue as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit.record("notes.add", {"subject": principal.subject, "note_id": item["id"], "kind": item["kind"]}); return item
@app.put("/api/notes/{note_id}")
async def update_note(note_id: str, body: NoteBody, principal: Principal = Depends(authenticated)):
    try: item = notes.update(principal.subject, note_id, **body.model_dump())
    except SecretLikeValue as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc
    if item is None: raise HTTPException(status_code=404, detail="note_not_found")
    audit.record("notes.update", {"subject": principal.subject, "note_id": note_id}); return item
@app.delete("/api/notes/{note_id}")
async def delete_note(note_id: str, principal: Principal = Depends(authenticated)):
    if not notes.delete(principal.subject, note_id): raise HTTPException(status_code=404, detail="note_not_found")
    audit.record("notes.delete", {"subject": principal.subject, "note_id": note_id}); return {"ok": True, "id": note_id}
@app.get("/api/scratchpad")
async def legacy_scratchpad(principal: Principal = Depends(authenticated)):
    items = notes.list(principal.subject, limit=50); return {"items": items, "count": len(items), "mode": "notes_only"}

@app.post("/api/decision")
async def decision(body: DecisionRequest, principal: Principal = Depends(authenticated)):
    history = [{"role": message.role, "content": message.text_content()} for message in body.history]
    # If Brain is wired in, delegate the kernel decision to it. The
    # Python kernel stays as a fallback when Brain is disabled.
    if brain_client.is_configured():
        body_out, latency_ms, status = await brain_client.timed_decision(user_input=body.user_input, history=history, metadata={"source": "aion.python.backend", "app_version": settings.app_version, "subject": principal.subject})
        if status == "active":
            audit.record("kernel.decision.brain", {"subject": principal.subject, "state": body_out.get("decision", {}).get("state"), "latency_ms": latency_ms})
            return JSONResponse(
                status_code=200,
                content=body_out,
                headers={"X-AION-Brain": "active", "X-AION-Brain-Latency-Ms": str(latency_ms), "X-AION-Brain-Decision": body_out.get("decision", {}).get("state", "UNKNOWN")},
            )
        # status == "down" — record + check brain_required
        audit.record("kernel.decision.brain.failed", {"subject": principal.subject, "error": body_out.get("error"), "latency_ms": latency_ms})
        if settings.brain_required:
            return JSONResponse(status_code=200, content=body_out, headers={"X-AION-Brain": "down", "X-AION-Brain-Latency-Ms": str(latency_ms)})
        # Fall through to local kernel
    elif not settings.brain_enabled:
        # User explicitly turned off Brain. Record the SSE event for the
        # topbar pill so the frontend can show BRAIN OFF.
        audit.record("kernel.decision.brain.disabled", {"subject": principal.subject})
    context = MissionContext(user_input=body.user_input, history=history)
    result = resolve_decision(context)
    audit.record("kernel.decision", {"subject": principal.subject, "request_id": context.request_id, "state": result.state.value})
    return {"request_id": context.request_id, "decision": result.to_dict()}
@app.post("/api/search")
async def search(body: SearchBody, _: Principal = Depends(authenticated)):
    results = await web_search.search(body.query, count=body.count, freshness=body.freshness); return {"query": body.query, "results": [result.__dict__ for result in results], "count": len(results)}
async def _github_ready() -> None:
    if not (settings.github_token or settings.github_app_configured):
        raise ToolConfigurationError("github_not_configured")

@app.post("/api/github/repository")
async def github_repository(body: GitHubRepoBody, _: Principal = Depends(authenticated)):
    await _github_ready()
    return await github.get_repository(body.repository)
@app.post("/api/github/file")
async def github_file(body: GitHubFileBody, _: Principal = Depends(authenticated)):
    await _github_ready()
    return await github.get_file(body.repository, body.path, body.ref)
@app.post("/api/github/issues")
async def github_issues(body: GitHubRepoBody, _: Principal = Depends(authenticated)):
    await _github_ready()
    items = await github.list_issues(body.repository); return {"items": items, "count": len(items)}
@app.post("/api/github/search")
async def github_search(body: GitHubSearchBody, _: Principal = Depends(authenticated)):
    await _github_ready()
    items = await github.search_code(body.repository, body.query, limit=body.limit); return {"items": items, "count": len(items)}
@app.post("/api/github/issues/create")
async def github_create_issue(body: GitHubIssueWrite, principal: Principal = Depends(confirmed_admin)):
    result = await github.create_issue(body.repository, body.title, body.body); audit.record("github.issue_created", {"subject": principal.subject, "repository": body.repository, "number": result["number"]}); return result
@app.post("/api/github/branches/create")
async def github_create_branch(body: GitHubBranchWrite, principal: Principal = Depends(confirmed_admin)):
    result = await github.create_branch(body.repository, body.branch, body.base); audit.record("github.branch_created", {"subject": principal.subject, "repository": body.repository, "branch": body.branch}); return result
@app.post("/api/github/files/upsert")
async def github_upsert_file(body: GitHubFileWrite, principal: Principal = Depends(confirmed_admin)):
    result = await github.upsert_file(body.repository, body.path, body.content, body.message, body.branch); audit.record("github.file_upserted", {"subject": principal.subject, "repository": body.repository, "path": body.path}); return result
@app.post("/api/github/pulls/create")
async def github_create_pull(body: GitHubPullWrite, principal: Principal = Depends(confirmed_admin)):
    result = await github.create_pull_request(body.repository, body.title, body.body, body.head, body.base); audit.record("github.pull_created", {"subject": principal.subject, "repository": body.repository, "number": result["number"]}); return result
@app.post("/api/tts")
async def tts(body: dict[str, Any], _: Principal = Depends(authenticated)):
    text = str(body.get("text") or "").strip()
    if not text: raise HTTPException(status_code=400, detail="text_required")
    voice = str(body.get("voice") or "alloy").strip() or "alloy"
    fmt = str(body.get("format") or "mp3").strip() or "mp3"
    text = text[:settings.max_message_chars]
    if not settings.openai_api_key: return {"ok": False, "error": "openai_not_configured", "text": text, "voice": voice, "format": fmt, "mode": "client_fallback"}
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url, timeout=settings.request_timeout_seconds)
        response = await client.audio.speech.create(model="gpt-4o-mini-tts", voice=voice, input=text, response_format=fmt)
        audio_bytes = response.read()
        import base64
        return {"ok": True, "mode": "server", "text": text, "voice": voice, "format": fmt, "audio_b64": base64.b64encode(audio_bytes).decode("ascii"), "size_bytes": len(audio_bytes)}
    except Exception as exc:
        # DO Cloudflare edge wraps 5xx as HTML 504 — return 200+ok=false
        audit.record("tts.failed", {"error": str(exc)[:200], "voice": voice, "text_len": len(text)})
        return {"ok": False, "error": f"tts_failed: {exc}", "kind": "tts_error", "text": text, "voice": voice, "format": fmt, "mode": "client_fallback"}


class ImageGenBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    model: str = Field(default="gpt-image-1", max_length=80)
    size: str = Field(default="1024x1024", max_length=20)
    n: int = Field(default=1, ge=1, le=4)


@app.post("/api/image/generate")
async def image_generate(body: ImageGenBody, principal: Principal = Depends(authenticated)):
    if not settings.openai_api_key: return {"ok": False, "error": "openai_not_configured", "model": body.model}
    audit.record("image.generate.started", {"subject": principal.subject, "model": body.model, "size": body.size, "n": body.n, "prompt_hash": _hash_text(body.prompt)})
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url, timeout=settings.request_timeout_seconds)
        kwargs: dict[str, Any] = {"model": body.model, "prompt": body.prompt, "n": body.n}
        if body.model.startswith("dall-e"): kwargs["size"] = body.size
        else: kwargs["size"] = body.size
        response = await client.images.generate(**kwargs)
        import base64
        items: list[dict[str, Any]] = []
        gallery_ids: list[str] = []
        for idx, item in enumerate(response.data or []):
            record: dict[str, Any] = {"revised_prompt": getattr(item, "revised_prompt", None)}
            b64 = getattr(item, "b64_json", None)
            url = getattr(item, "url", None)
            if b64: record["b64_json"] = b64
            if url: record["url"] = url
            items.append(record)
            # Persist to gallery
            try:
                w, h = _parse_size(body.size) if "x" in body.size else (None, None)
                gal = gallery.add(
                    owner=principal.subject, kind="image", source="openai",
                    mime="image/png" if body.model != "dall-e-3" else "image/png",
                    filename=f"image_{int(time.time())}_{idx}.png",
                    prompt=body.prompt, model=body.model, size=body.size,
                    width=w, height=h,
                    b64=b64, external_url=url,
                    metadata={"revised_prompt": getattr(item, "revised_prompt", None)},
                )
                record["gallery_id"] = gal.id
                gallery_ids.append(gal.id)
            except Exception as exc: audit.record("gallery.persist_failed", {"subject": principal.subject, "model": body.model, "error": str(exc)[:200]})
        audit.record("image.generate.succeeded", {"subject": principal.subject, "model": body.model, "count": len(items), "gallery_ids": gallery_ids})
        return {"ok": True, "model": body.model, "items": items, "count": len(items), "gallery_ids": gallery_ids}
    except Exception as exc:
        # DO Cloudflare edge wraps 5xx as HTML 504 — return 200+ok=false
        return {"ok": False, "error": f"image_generate_failed: {exc}", "kind": "image_error", "model": body.model, "items": [], "count": 0}


class VideoGenBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    model: str = Field(default="sora-2", max_length=80)
    seconds: int = Field(default=4, ge=2, le=20)
    size: str = Field(default="1280x720", max_length=20)
    input_reference: str | None = Field(default=None, max_length=2_000_000)
    poll: bool = Field(default=False)
    poll_timeout_seconds: int = Field(default=120, ge=5, le=600)


def _parse_size(value: str) -> tuple[int, int]:
    try:
        w_str, h_str = value.lower().split("x", 1)
        return int(w_str), int(h_str)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid_size_format:{value}") from exc


@app.post("/api/video/generate")
async def video_generate(body: VideoGenBody, principal: Principal = Depends(authenticated)):
    if not settings.openai_api_key: return {"ok": False, "error": "openai_not_configured", "model": body.model}
    audit.record("video.generate.started", {"subject": principal.subject, "model": body.model, "size": body.size, "seconds": body.seconds, "poll": body.poll, "prompt_hash": _hash_text(body.prompt)})
    width, height = _parse_size(body.size)
    import httpx
    headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
    timeout = max(30, body.poll_timeout_seconds + 30)
    async with httpx.AsyncClient(base_url=settings.openai_base_url.rstrip("/"), timeout=timeout, follow_redirects=False) as client:
        # Create the job
        create_headers = dict(headers)
        if body.input_reference:
            create_headers.pop("Content-Type", None)
            files: dict[str, Any] = {"input_reference": ("first_frame.png", _decode_data_url(body.input_reference), "image/png")}
            data: dict[str, Any] = {"model": body.model, "prompt": body.prompt, "seconds": str(body.seconds), "size": body.size}
            create_resp = await client.post("/videos", headers=create_headers, files=files, data=data)
        else:
            create_headers["Content-Type"] = "application/json"
            create_body = {"model": body.model, "prompt": body.prompt, "seconds": str(body.seconds), "size": body.size}
            create_resp = await client.post("/videos", headers=create_headers, json=create_body)
        if create_resp.status_code >= 400:
            detail = _extract_error(create_resp)
            if create_resp.status_code in (400, 403, 404) and ("model" in detail.lower() or "permission" in detail.lower() or "not found" in detail.lower()):
                audit.record("video.generate.fallback", {"subject": principal.subject, "model": body.model, "reason": detail[:120]})
                return {"ok": False, "fallback": "image_to_video", "reason": "sora_unavailable", "message": detail, "model": body.model}
            # Any non-2xx from OpenAI = upstream error. Convert to clean
            # 200+ok=false so the client gets parseable JSON (DO Cloudflare
            # edge would otherwise wrap 5xx as HTML 504).
            return {"ok": False, "error": f"video_create_failed: {detail}", "kind": "video_error", "model": body.model, "upstream_status": create_resp.status_code, "fallback": "image_to_video", "reason": "sora_unavailable"}
        job = create_resp.json()
        video_id = job.get("id")
        status = job.get("status", "queued")
        if not body.poll or status in ("completed", "failed"):
            # Persist metadata to gallery so the operator can find the job
            gallery_id: str | None = None
            if status == "completed":
                try:
                    w, h = (body.size.split("x") + [None, None])[:2]
                    gal = gallery.add(
                        owner=principal.subject, kind="video", source="sora",
                        mime="video/mp4",
                        filename=f"video_{video_id}.mp4",
                        prompt=body.prompt, model=body.model, size=body.size,
                        width=int(w) if w else None, height=int(h) if h else None,
                        seconds=body.seconds,
                        external_id=video_id,
                        metadata={"progress": job.get("progress"), "raw": {k: v for k, v in (job or {}).items() if k != "content"}},
                    )
                    gallery_id = gal.id
                except Exception as exc: audit.record("gallery.persist_failed", {"subject": principal.subject, "model": body.model, "video_id": video_id, "error": str(exc)[:200]})
            audit.record("video.generate.succeeded", {"subject": principal.subject, "model": body.model, "video_id": video_id, "status": status, "polled": False, "gallery_id": gallery_id})
            return {"ok": True, "model": body.model, "video_id": video_id, "status": status, "size": body.size, "seconds": body.seconds, "progress": job.get("progress"), "raw": job, "gallery_id": gallery_id}
        # Poll until done
        import asyncio
        deadline = asyncio.get_event_loop().time() + body.poll_timeout_seconds
        last_job = job
        while True:
            if asyncio.get_event_loop().time() > deadline:
                audit.record("video.generate.timeout", {"subject": principal.subject, "model": body.model, "video_id": video_id})
                return {"ok": True, "model": body.model, "video_id": video_id, "status": last_job.get("status"), "progress": last_job.get("progress"), "timed_out": True, "message": "Job still running. Poll GET /api/video/{video_id} for status."}
            await asyncio.sleep(5)
            get_resp = await client.get(f"/videos/{video_id}", headers=headers)
            if get_resp.status_code >= 500:
                audit.record("video.generate.failed", {"subject": principal.subject, "model": body.model, "video_id": video_id, "stage": "poll", "error": _extract_error(get_resp)[:200]})
                return {"ok": False, "error": f"video_status_failed: {_extract_error(get_resp)}", "kind": "video_error", "model": body.model, "video_id": video_id, "upstream_status": get_resp.status_code}
            if get_resp.status_code >= 400:
                audit.record("video.generate.failed", {"subject": principal.subject, "model": body.model, "video_id": video_id, "error": _extract_error(get_resp)[:200]})
                raise HTTPException(status_code=get_resp.status_code, detail=f"video_status_failed: {_extract_error(get_resp)}")
            last_job = get_resp.json()
            if last_job.get("status") in ("completed", "failed", "cancelled"):
                break
        if last_job.get("status") != "completed":
            audit.record("video.generate.failed", {"subject": principal.subject, "model": body.model, "video_id": video_id, "status": last_job.get("status")})
            return {"ok": False, "model": body.model, "video_id": video_id, "status": last_job.get("status"), "error": last_job.get("error"), "raw": last_job}
        # Download the MP4
        content_resp = await client.get(f"/videos/{video_id}/content", headers=headers)
        if content_resp.status_code >= 500:
            audit.record("video.generate.failed", {"subject": principal.subject, "model": body.model, "video_id": video_id, "stage": "download", "error": _extract_error(content_resp)[:200]})
            return {"ok": False, "error": f"video_download_failed: {_extract_error(content_resp)}", "kind": "video_error", "model": body.model, "video_id": video_id, "upstream_status": content_resp.status_code}
        if content_resp.status_code >= 400:
            audit.record("video.generate.failed", {"subject": principal.subject, "model": body.model, "video_id": video_id, "stage": "download", "error": _extract_error(content_resp)[:200]})
            raise HTTPException(status_code=content_resp.status_code, detail=f"video_download_failed: {_extract_error(content_resp)}")
        import base64
        mp4_bytes = content_resp.content
        # Persist to gallery
        gallery_id: str | None = None
        try:
            w, h = (body.size.split("x") + [None, None])[:2]
            gal = gallery.add(
                owner=principal.subject, kind="video", source="sora",
                mime="video/mp4",
                filename=f"video_{video_id}.mp4",
                prompt=body.prompt, model=body.model, size=body.size,
                width=int(w) if w else None, height=int(h) if h else None,
                seconds=body.seconds,
                data=mp4_bytes, external_id=video_id,
                metadata={"last_job": {k: v for k, v in (last_job or {}).items() if k != "content"}},
            )
            gallery_id = gal.id
        except Exception as exc: audit.record("gallery.persist_failed", {"subject": principal.subject, "model": body.model, "video_id": video_id, "error": str(exc)[:200]})
        audit.record("video.generate.succeeded", {"subject": principal.subject, "model": body.model, "video_id": video_id, "size_bytes": len(mp4_bytes), "polled": True, "gallery_id": gallery_id})
        return {"ok": True, "model": body.model, "video_id": video_id, "status": "completed", "size": body.size, "seconds": body.seconds, "mp4_b64": base64.b64encode(mp4_bytes).decode("ascii"), "size_bytes": len(mp4_bytes), "raw": {k: v for k, v in last_job.items() if k != "content"}, "gallery_id": gallery_id}


@app.get("/api/video/{video_id}")
async def video_status(video_id: str, _: Principal = Depends(authenticated)):
    if not settings.openai_api_key: return {"ok": False, "error": "openai_not_configured", "video_id": video_id}
    import httpx
    async with httpx.AsyncClient(base_url=settings.openai_base_url.rstrip("/"), timeout=30, follow_redirects=False) as client:
        resp = await client.get(f"/videos/{video_id}", headers={"Authorization": f"Bearer {settings.openai_api_key}"})
        if resp.status_code >= 500:
            # DO Cloudflare edge wraps 5xx as HTML 504 — return 200+ok=false
            return {"ok": False, "error": _extract_error(resp), "kind": "video_status_error", "video_id": video_id, "upstream_status": resp.status_code}
        if resp.status_code >= 400: raise HTTPException(status_code=resp.status_code, detail=_extract_error(resp))
        return resp.json()


@app.get("/api/video/{video_id}/content")
async def video_content(video_id: str, _: Principal = Depends(authenticated)):
    if not settings.openai_api_key: return {"ok": False, "error": "openai_not_configured", "video_id": video_id}
    import httpx
    async with httpx.AsyncClient(base_url=settings.openai_base_url.rstrip("/"), timeout=120, follow_redirects=False) as client:
        resp = await client.get(f"/videos/{video_id}/content", headers={"Authorization": f"Bearer {settings.openai_api_key}"})
        if resp.status_code >= 500:
            # DO Cloudflare edge wraps 5xx as HTML 504 — return 200+ok=false
            return {"ok": False, "error": _extract_error(resp), "kind": "video_content_error", "video_id": video_id, "upstream_status": resp.status_code}
        if resp.status_code >= 400: raise HTTPException(status_code=resp.status_code, detail=_extract_error(resp))
        import base64
        return {"ok": True, "video_id": video_id, "mp4_b64": base64.b64encode(resp.content).decode("ascii"), "size_bytes": len(resp.content)}


def _extract_error(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            if "error" in data:
                err = data["error"]
                if isinstance(err, dict): return err.get("message") or str(err)
                return str(err)
            return data.get("message") or data.get("detail") or str(data)[:300]
        return str(data)[:300]
    except Exception: return response.text[:300] or f"http_{response.status_code}"


def _decode_data_url(value: str) -> bytes:
    import base64, binascii, re
    match = re.match(r"^data:image/(?P<kind>png|jpeg|webp|gif);base64,(?P<data>.*)$", value, re.I | re.S)
    if not match: raise HTTPException(status_code=400, detail="input_reference_must_be_data_image_url")
    try: return base64.b64decode(match.group("data"), validate=False)
    except binascii.Error as exc: raise HTTPException(status_code=400, detail="input_reference_base64_invalid") from exc


def _safe_json(obj: Any) -> Any:
    try:
        if hasattr(obj, "model_dump"): return obj.model_dump()
        if hasattr(obj, "to_dict"): return obj.to_dict()
        if hasattr(obj, "__dict__"): return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
        return obj
    except Exception: return None


def _hash_text(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.encode()).hexdigest()[:16]

@app.post("/api/chat")
async def chat(body: ChatRequest, principal: Principal = Depends(authenticated)):
    last_user = next((message for message in reversed(body.messages) if message.role == "user"), None)
    if last_user is None: raise HTTPException(status_code=400, detail="user_message_required")
    user_text = last_user.text_content(); tool_contexts = []; tool_events = []; tool_errors = []
    search_query = _search_query(body.web_search, user_text)
    if search_query:
        try:
            results = await web_search.search(search_query); context = web_search.as_context(results)
            if context: tool_contexts.append(context)
            tool_events.append({"type": "tool", "tool": "web_search", "query": search_query, "results": [result.__dict__ for result in results]})
        except (ToolConfigurationError, ToolRequestError) as exc: tool_errors.append(str(exc)); tool_events.append({"type": "tool_error", "tool": "web_search", "message": str(exc)})
    repository, github_mode, github_argument = _github_request(body, user_text)
    if repository:
        try:
            if github_mode == "file" and github_argument: result = await github.get_file(repository, github_argument)
            elif github_mode == "search" and github_argument: result = await github.search_code(repository, github_argument)
            elif github_mode == "issues": result = await github.list_issues(repository)
            else: result = await github.get_repository(repository)
            tool_contexts.append(github.as_context(github_mode, repository, result)); tool_events.append({"type": "tool", "tool": f"github_{github_mode}", "repository": repository, "result": result})
        except (ToolConfigurationError, ToolRequestError) as exc: tool_errors.append(str(exc)); tool_events.append({"type": "tool_error", "tool": "github", "message": str(exc)})
    mission = MissionContext(user_input=user_text or "image attachment", history=[{"role": message.role, "content": message.text_content()} for message in body.messages[:-1]], metadata={"web_search": bool(search_query), "github": bool(repository), "tool_context_available": bool(tool_contexts), "tool_errors": tool_errors})
    decision_result = resolve_decision(mission); system_prompt = build_system_prompt(decision_result, tool_context="\n\n".join(tool_contexts), notes_context=notes.context(principal.subject, user_text) if body.use_notes else "", tool_errors=tuple(tool_errors))
    model_messages = [{"role": "system", "content": system_prompt}, *({"role": message.role, "content": message.wire_content()} for message in body.messages)]
    model_messages = [model_messages[0], *model_messages[1:][-(settings.max_context_messages - 1):]]
    try: model_chain = await resolve_model_chain(body.provider, body.model)
    except InvalidModelSelection as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AllProvidersFailed as exc: return JSONResponse(status_code=200, content={"ok": False, "error": str(exc), "kind": "all_providers_failed"})
    audit.record("chat.started", {"subject": principal.subject, "request_id": mission.request_id, "message_count": len(body.messages), "web_search": bool(search_query), "github": bool(repository), "brain": brain_client.is_configured()})
    # If a tool was requested and errored, the local kernel has decided
    # DEFER. That decision is authoritative — Brain is the LLM router,
    # not the tool-state oracle. Don't ask Brain to re-decide.
    tool_state_forced_defer = bool(tool_errors) and bool(search_query or repository)
    use_brain = brain_client.is_configured() and not settings.brain_decision_only and not tool_state_forced_defer  # brain_decision_only means: use brain for /api/decision only, not /api/chat
    # Probe Brain once to capture latency for the SSE brain event + the
    # X-AION-Brain-Latency-Ms response header. Done synchronously here so
    # the SSE brain event carries the right value; the chat stream itself
    # does the real work below.
    brain_probe_at_decision: dict[str, Any] = {}
    if brain_client.is_configured():
        brain_probe_at_decision = await brain_client.probe_brain(timeout_seconds=2.0)
    # Also surface tool errors to Brain's decision so it could DEFER too
    # (currently we short-circuit above when tool_state_forced_defer, but
    # this keeps Brain's audit trail honest).
    brain_metadata_extras: dict[str, Any] = {"tool_errors": tool_errors} if tool_errors else {}
    async def events() -> AsyncIterator[bytes]:
        # Brain-link signal (BEFORE decision so the UI lights up first).
        if brain_client.is_configured():
            if brain_probe_at_decision.get("reachable"):
                yield _sse(brain_client.brain_sse.active(brain_probe_at_decision.get("latency_ms") or 0))
            else:
                yield _sse(brain_client.brain_sse.down(brain_probe_at_decision.get("error") or "unreachable", brain_probe_at_decision.get("latency_ms")))
        else:
            yield _sse(brain_client.brain_sse.disabled())
        yield _sse({"type": "decision", "request_id": mission.request_id, "decision": decision_result.to_dict()})
        for event in tool_events:
            yield _sse(event)

        # Hard stop on tool failure: never let the LLM invent analysis
        # without evidence. Stream a factual refusal and return — Brain,
        # OpenAI, NVIDIA, the whole chain is skipped for this turn.
        if tool_errors and (search_query or repository):
            text = _defer_tool_failure_text(
                tool_errors,
                repository=repository or "",
                search_query=search_query or "",
            )
            yield _sse({"type": "open", "provider": "aion", "model": "defer-gate"})
            chunk = 48
            for i in range(0, len(text), chunk):
                yield _sse({"type": "delta", "text": text[i : i + chunk]})
                await asyncio.sleep(0)
            yield _sse({
                "type": "done",
                "streaming": False,
                "provider": "aion",
                "model": "defer-gate",
                "finish_reason": "defer_tool_failure",
            })
            audit.record(
                "chat.deferred_tool_failure",
                {
                    "subject": principal.subject,
                    "request_id": mission.request_id,
                    "errors": tool_errors[:5],
                    "repository": repository or None,
                },
            )
            yield b"data: [DONE]\n\n"
            return

        if use_brain:
            # Stream from Brain. AION keeps the local system prompt, tool
            # results, and notes context. Brain does the 7-law re-decision
            # (if it wants) and the LLM provider chain.
            # Brain only accepts user/assistant roles. We fold the system
            # prompt into the first user message so Brain sees a single
            # user turn containing the kernel + tools + question.
            brain_messages: list[dict[str, Any]] = []
            for m in model_messages:
                role = m.get("role")
                content = m.get("content")
                if role == "system":
                    if brain_messages:
                        # Prepend to the previous user message
                        if brain_messages[0]["role"] == "user":
                            brain_messages[0]["content"] = f"{content}\n\n{brain_messages[0]['content']}"
                        else:
                            brain_messages.insert(0, {"role": "user", "content": content})
                    else:
                        brain_messages.append({"role": "user", "content": content})
                else:
                    brain_messages.append({"role": role, "content": content})
            try:
                async with limiter.chat_slot():
                    async for evt in brain_client.stream_chat(messages=brain_messages, temperature=body.temperature, max_tokens=body.max_tokens, model=body.model, provider=body.provider):
                        if evt.get("type") == "[DONE]":
                            yield b"data: [DONE]\n\n"; return
                        # Forward all Brain SSE events unchanged. The
                        # frontend already understands the AION v2 contract.
                        yield _sse(evt); await asyncio.sleep(0)
                    yield b"data: [DONE]\n\n"
                return
            except brain_client.BrainUnavailable as exc:
                if settings.brain_required:
                    yield _sse({"type": "error", "kind": "brain_unavailable", "message": str(exc)[:200]})
                    audit.record("chat.failed", {"subject": principal.subject, "request_id": mission.request_id, "error": str(exc)[:200]})
                    yield b"data: [DONE]\n\n"; return
                # Fall through to local LLM chain
                audit.record("chat.brain_fallback", {"subject": principal.subject, "request_id": mission.request_id, "reason": str(exc)[:200]})
            except brain_client.BrainAuthRejected as exc:
                yield _sse({"type": "error", "kind": "brain_auth_rejected", "message": str(exc)[:200]})
                audit.record("chat.failed", {"subject": principal.subject, "request_id": mission.request_id, "error": "brain_auth_rejected"})
                yield b"data: [DONE]\n\n"; return
            except brain_client.BrainBadResponse as exc:
                yield _sse({"type": "error", "kind": "brain_bad_response", "message": str(exc)[:200]})
                audit.record("chat.failed", {"subject": principal.subject, "request_id": mission.request_id, "error": "brain_bad_response"})
                yield b"data: [DONE]\n\n"; return
        # Local LLM chain path (also used as fallback when Brain is down)
        async with limiter.chat_slot():
            try:
                async for event_type, payload in stream_chat(model_chain=model_chain, messages=model_messages, temperature=body.temperature, max_tokens=body.max_tokens, request_id=mission.request_id):
                    yield _sse({"type": event_type, **payload}); await asyncio.sleep(0)
            except AllProvidersFailed as exc:
                yield _sse({"type": "error", "kind": "all_providers_failed", "message": str(exc)}); audit.record("chat.failed", {"subject": principal.subject, "request_id": mission.request_id, "error": str(exc)})
        yield b"data: [DONE]\n\n"
    stream_headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no", "X-Content-Type-Options": "nosniff", "X-AION-Decision": decision_result.state.value}
    if brain_probe_at_decision:
        stream_headers["X-AION-Brain"] = "active" if brain_probe_at_decision.get("reachable") else "down"
        if brain_probe_at_decision.get("latency_ms") is not None:
            stream_headers["X-AION-Brain-Latency-Ms"] = str(brain_probe_at_decision.get("latency_ms"))
        if brain_probe_at_decision.get("reachable") is False and brain_probe_at_decision.get("error"):
            stream_headers["X-AION-Brain-Error"] = str(brain_probe_at_decision.get("error"))[:200]
    return StreamingResponse(events(), media_type="text/event-stream", headers=stream_headers)

# ===========================================================================
# Vault — admin-scoped secret management
# ===========================================================================

class VaultRotateBody(BaseModel):
    value: str = Field(min_length=1, max_length=8000)
    metadata: dict[str, Any] | None = None

@app.get("/api/vault/status")
async def vault_status(_: Principal = Depends(require_admin)):
    """Aggregate status: total/configured counts, key derivation flag, etc."""
    return {"ok": True, **vault.status()}

@app.get("/api/vault")
async def vault_list(category: str | None = Query(default=None, max_length=40), _: Principal = Depends(require_admin)):
    """List all known keys. NEVER returns plaintext values."""
    entries = vault.list_entries(category=category)
    return {"ok": True, "items": [e.public_dict() for e in entries], "count": len(entries), "known_keys": len(VAULT_KNOWN_KEYS)}

@app.get("/api/vault/known")
async def vault_known(_: Principal = Depends(require_admin)):
    """Return the catalogue of known key names (independent of whether they are configured)."""
    return {"ok": True, "keys": VAULT_KNOWN_KEYS, "count": len(VAULT_KNOWN_KEYS)}

@app.post("/api/vault/{name}/reveal")
async def vault_reveal(name: str, principal: Principal = Depends(confirmed_admin)):
    """Decrypt and return the plaintext value of a single key. Logged."""
    try:
        plaintext = vault.reveal(name)
    except VaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if plaintext is None:
        raise HTTPException(status_code=404, detail="key_not_found_or_empty")
    audit.record("vault.revealed", {"subject": principal.subject, "name": name})
    return {"ok": True, "name": name, "value": plaintext, "fingerprint": _fingerprint(plaintext)}

@app.post("/api/vault/{name}/rotate")
async def vault_rotate(name: str, body: VaultRotateBody, principal: Principal = Depends(confirmed_admin)):
    """Set a new value for a known key. Writes to encrypted DB and to live env (hot-reload)."""
    try:
        entry = vault.set_value(name=name, value=body.value, actor=principal.subject, source="rotate", metadata=body.metadata)
    except VaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit.record("vault.rotated", {"subject": principal.subject, "name": name, "fingerprint": entry.fingerprint})
    return {"ok": True, "entry": entry.public_dict()}

@app.post("/api/vault/{name}/ping")
async def vault_ping_one(name: str, principal: Principal = Depends(require_admin)):
    """Ping a single provider. Records the result on the entry."""
    try:
        plaintext = vault.reveal(name)
    except VaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if plaintext is None: raise HTTPException(status_code=404, detail="key_not_found_or_empty")
    from .vault import ping as _ping
    ok, latency_ms, error = await _ping(name, plaintext)
    vault.record_ping(name, ok=ok, latency_ms=latency_ms, error=error)
    audit.record("vault.pinged", {"subject": principal.subject, "name": name, "ok": ok, "latency_ms": latency_ms})
    return {"ok": ok, "name": name, "latency_ms": latency_ms, "error": error}

@app.post("/api/vault/ping")
async def vault_ping_all(_: Principal = Depends(require_admin)):
    """Ping every configured key in parallel. Returns per-key result + overall summary."""
    results = await ping_all()
    ok_count = sum(1 for r in results if r.get("ok"))
    err_count = len(results) - ok_count
    audit.record("vault.ping_all", {"ok": ok_count, "error": err_count, "total": len(results)})
    return {"ok": True, "results": results, "summary": {"total": len(results), "ok": ok_count, "error": err_count}}

@app.post("/api/vault/reconcile")
async def vault_reconcile(principal: Principal = Depends(require_admin)):
    """Re-import any newly-set env vars into the vault (does not overwrite manual values)."""
    seeded = vault.reconcile_with_env(actor=f"admin:{principal.subject}")
    audit.record("vault.reconciled", {"subject": principal.subject, "seeded": seeded})
    return {"ok": True, "seeded": seeded, "known_keys": len(VAULT_KNOWN_KEYS)}

@app.delete("/api/vault/{name}")
async def vault_delete(name: str, principal: Principal = Depends(confirmed_admin)):
    """Fully remove a key from the vault. Also clears the live env alias so
    the running app stops using it."""
    info = next((k for k in VAULT_KNOWN_KEYS if k["name"] == name), None)
    if info is None:
        raise HTTPException(status_code=404, detail=f"unknown_key: {name}")
    removed = vault.delete_value(name)
    if not removed:
        raise HTTPException(status_code=404, detail="key_not_found")
    # Clear the live env alias so the running app stops using the deleted key.
    env_alias = info.get("env_aliases") or name
    os.environ.pop(env_alias, None)
    audit.record("vault.deleted", {"subject": principal.subject, "name": name, "env_alias": env_alias})
    return {"ok": True, "name": name, "deleted": True, "env_cleared": env_alias}

# ===========================================================================
# Gallery — persistent image + video store
# ===========================================================================


# ===========================================================================
# Gallery — persistent image + video store
# ===========================================================================

@app.get("/api/gallery/status")
async def gallery_status(_: Principal = Depends(authenticated)):
    return gallery.status()

@app.get("/api/gallery")
async def gallery_list(kind: Literal["image", "video"] | None = None, limit: int = Query(default=60, ge=1, le=200), offset: int = Query(default=0, ge=0), principal: Principal = Depends(authenticated)):
    items = gallery.list(principal.subject, kind=kind, limit=limit, offset=offset)
    return {"ok": True, "items": [i.public_dict() for i in items], "count": len(items)}

@app.delete("/api/gallery/{item_id}")
async def gallery_delete(item_id: str, principal: Principal = Depends(authenticated)):
    if not gallery.delete(principal.subject, item_id): raise HTTPException(status_code=404, detail="item_not_found")
    audit.record("gallery.deleted", {"subject": principal.subject, "item_id": item_id})
    return {"ok": True, "id": item_id}

@app.get("/api/gallery/{item_id}/raw")
async def gallery_raw(item_id: str, principal: Principal = Depends(authenticated)):
    """Stream the raw bytes (image/png or video/mp4) for a gallery item. Owner-scoped."""
    item = gallery.get(item_id)
    if item is None or item.owner != principal.subject: raise HTTPException(status_code=404, detail="item_not_found")
    data = gallery.get_data(item_id)
    if data is None: raise HTTPException(status_code=404, detail="data_not_found")
    from fastapi.responses import Response
    return Response(content=data, media_type=item.mime, headers={"Cache-Control": "private, max-age=300", "X-AION-Filename": item.filename, "Content-Disposition": f'inline; filename="{item.filename}"'})

def _sse(payload): return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()
def _search_query(enabled, text):
    stripped = text.strip(); return stripped[8:].strip()[:400] if stripped.lower().startswith("/search ") else stripped[:400] if enabled else ""
def _defer_tool_failure_text(tool_errors, *, repository: str = "", search_query: str = "") -> str:
    """Hardcoded refusal streamed to the client when the kernel DEFERs
    because a tool was requested and errored. The LLM never sees this
    prompt — we stream this directly so the model can't invent filler.
    """
    lines = [
        "DEFER — external evidence was required and the tool failed.",
        "I will not invent a repository review or generic checklist without reads.",
        "",
        "Tool errors:",
    ]
    for err in tool_errors:
        lines.append(f"- {err}")
    if repository:
        lines.append("")
        lines.append(f"Requested repository: `{repository}`")
        joined = " ".join(tool_errors)
        if "github_repository_not_allowed" in joined:
            lines.append(
                "This repo is outside `GITHUB_ALLOWED_REPOSITORIES`. "
                "An operator must allowlist it, or you can paste the README / file tree here."
            )
        elif "github_not_configured" in joined:
            lines.append("GitHub is not configured on this backend (missing token/app).")
        else:
            lines.append("See the error above for the cause.")
    if search_query:
        lines.append(f"Search query: {search_query}")
    lines.append("")
    lines.append(
        "Next step: allowlist the repo, fix tool config, or paste the source text you want reviewed."
    )
    return "\n".join(lines)


def _github_request(body, text):
    repository = (body.github_repository or "").strip(); mode = "repository"; argument = ""; stripped = (text or "").strip()
    if stripped.lower().startswith("/github "):
        command = stripped[8:].strip().split(maxsplit=2)
        if command: repository = command[0]
        if len(command) >= 2:
            action = command[1].lower()
            if action in {"issues", "repo", "repository"}: mode = "issues" if action == "issues" else "repository"
            elif action == "file" and len(command) == 3: mode, argument = "file", command[2]
            elif action == "search" and len(command) == 3: mode, argument = "search", command[2]
    elif getattr(body, "github_path", None): mode, argument = "file", body.github_path
    elif getattr(body, "github_query", None): mode, argument = "search", body.github_query
    elif not repository:
        match = _GITHUB_URL.search(text or "")
        if match: repository = f"{match.group('owner')}/{match.group('repo')}"
    if repository.startswith("http"):
        m = _GITHUB_URL.search(repository)
        if m: repository = f"{m.group('owner')}/{m.group('repo')}"
    repository = repository.removesuffix(".git")
    return repository, mode, argument
