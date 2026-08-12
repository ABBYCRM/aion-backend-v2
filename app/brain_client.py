"""Client for the Aion-Brain decision + chat kernel.

The Python AION backend delegates two responsibilities to Aion-Brain:

  1. **Decision** — `/api/decision` returns a 7-law decision (state,
     score, checks, protocol, id) for a single user_input + history.
     AION forwards the result up to the browser unchanged.

  2. **Chat stream** — `/api/chat` returns an SSE stream with the same
     event names the AION v2 backend already understands: `decision`,
     `attempt`, `open`, `delta`, `done`, `error`, `[DONE]`. AION
     proxies those events straight to the browser.

**Failure model — fail-closed, never crash the chat:**

- If AION_BRAIN_ENABLED is false, this module is a no-op. The existing
  Python LLM chain (kernel + LLM providers) is used as before.
- If Brain is unreachable, the chat route returns a controlled error
  event so the browser can show "decision kernel unreachable" instead
  of crashing.
- If AION_BRAIN_REQUIRED=true and Brain is down at boot, the
  application refuses to start. This is the only way to know the
  decision kernel is genuinely available.

The Brain is the source of truth for the 7 laws + provider chain when
it is enabled. AION keeps the heavy lifting (vault, notes, gallery,
GitHub, TTS, image, video, CORS, auth, rate limiting).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

import httpx

from .audit import audit
from .settings import settings


class BrainUnavailable(RuntimeError):
    """Raised when the Brain cannot be reached or returns a fatal error."""


class BrainAuthRejected(RuntimeError):
    """Raised when the Brain rejects the AION_BRAIN_KEY."""


class BrainBadResponse(RuntimeError):
    """Raised when the Brain returns a status we don't know how to handle."""


def is_configured() -> bool:
    """True if Aion-Brain is wired in (enabled + URL + key)."""
    # Always re-read the live settings in case the env was mutated
    # at runtime (e.g. by tests or by a rotate in the vault that
    # hot-reloads the env).
    import os
    enabled = os.getenv("AION_BRAIN_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    url = os.getenv("AION_BRAIN_URL", "").strip().rstrip("/")
    key = os.getenv("AION_BRAIN_KEY", "").strip()
    if enabled and url and key:
        return True
    # Fall back to the frozen settings (covers normal startup config).
    return bool(settings.brain_enabled and settings.brain_url and settings.brain_service_key)


def _headers() -> dict[str, str]:
    return {
        "X-AION-Key": settings.brain_service_key,
        "Content-Type": "application/json",
        "User-Agent": f"AION-Python/{settings.app_version}",
    }


def _base() -> str:
    return settings.brain_url.rstrip("/")


async def state() -> dict[str, Any]:
    """GET /api/state — used at boot to verify the Brain is the right
    version with the right providers. Raises on hard failure."""
    if not is_configured():
        raise BrainUnavailable("brain_not_configured")
    timeout = httpx.Timeout(settings.brain_timeout_ms / 1000.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{_base()}/api/state", headers=_headers())
    except httpx.HTTPError as exc:
        raise BrainUnavailable(f"brain_unreachable: {exc}") from exc
    if r.status_code == 401 or r.status_code == 403:
        raise BrainAuthRejected(f"brain_auth_rejected: {r.status_code}")
    if r.status_code >= 400:
        raise BrainBadResponse(f"brain_state_failed: {r.status_code} {r.text[:200]}")
    return r.json()


async def probe_brain(timeout_seconds: float = 3.0) -> dict[str, Any]:
    """Batched health probe for the UI topbar. Never raises; returns a
    dict the frontend can render directly.

    Shape (matches the install contract in docs/PROTOCOL.md):
        {
          "enabled": bool,
          "reachable": bool,
          "url_host": str | None,
          "latency_ms": int | None,
          "version": str | None,
          "last_decision": str | None,
          "agent_running": bool,
          "providers": list[str] | None,
          "primary_model": str | None,
          "error": str | None,
        }

    - If AION_BRAIN_ENABLED is false, returns enabled=False, reachable=False.
    - If AION_BRAIN_URL is missing, returns enabled=True, reachable=False,
      error="AION_BRAIN_URL not set".
    - Otherwise hits GET /healthz. On 2xx, also hits GET /api/state to
      pick up the version + providers + last decision.
    """
    if not settings.brain_enabled:
        return {"enabled": False, "reachable": False, "url_host": None, "latency_ms": None, "version": None, "last_decision": None, "agent_running": False, "providers": None, "primary_model": None, "error": "disabled"}
    if not settings.brain_url or not settings.brain_service_key:
        return {"enabled": True, "reachable": False, "url_host": None, "latency_ms": None, "version": None, "last_decision": None, "agent_running": False, "providers": None, "primary_model": None, "error": "AION_BRAIN_URL or AION_BRAIN_KEY not set"}
    url_host = settings.brain_url.split("://", 1)[-1].split("/", 1)[0]
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            r = await client.get(f"{_base()}/healthz", headers=_headers())
            latency = int((time.perf_counter() - started) * 1000)
            if r.status_code >= 400:
                return {"enabled": True, "reachable": False, "url_host": url_host, "latency_ms": latency, "version": None, "last_decision": None, "agent_running": False, "providers": None, "primary_model": None, "error": f"healthz_{r.status_code}"}
            version = None
            try:
                hbody = r.json()
                version = hbody.get("version")
            except Exception:
                pass
            # Best-effort state fetch
            providers = None
            primary_model = None
            try:
                s = await client.get(f"{_base()}/api/state", headers=_headers())
                if s.status_code == 200:
                    sj = s.json()
                    providers = sj.get("providers")
                    primary_model = sj.get("primary_model")
            except Exception:
                pass
            return {"enabled": True, "reachable": True, "url_host": url_host, "latency_ms": latency, "version": version, "last_decision": None, "agent_running": False, "providers": providers, "primary_model": primary_model, "error": None}
    except Exception as exc:
        latency = int((time.perf_counter() - started) * 1000)
        return {"enabled": True, "reachable": False, "url_host": url_host, "latency_ms": latency, "version": None, "last_decision": None, "agent_running": False, "providers": None, "primary_model": None, "error": f"{type(exc).__name__}: {exc}"[:200]}


async def timed_decision(*, user_input: str, history: list[dict[str, Any]] | None = None, metadata: dict[str, Any] | None = None) -> tuple[dict[str, Any], int, str]:
    """Wrapper around decision() that returns (body, latency_ms, status)
    so the caller can attach X-AION-Brain-* response headers."""
    started = time.time()
    try:
        body = await decision(user_input=user_input, history=history, metadata=metadata)
        return body, int((time.time() - started) * 1000), "active"
    except BrainUnavailable as exc:
        return {"ok": False, "error": str(exc), "kind": "brain_unavailable"}, int((time.time() - started) * 1000), "down"
    except BrainAuthRejected as exc:
        return {"ok": False, "error": str(exc), "kind": "brain_auth_rejected"}, int((time.time() - started) * 1000), "down"
    except BrainBadResponse as exc:
        return {"ok": False, "error": str(exc), "kind": "brain_bad_response"}, int((time.time() - started) * 1000), "down"


class _SseEvent:
    """Tiny helper to build a brain SSE event dict."""
    @staticmethod
    def active(latency_ms: int) -> dict[str, Any]:
        return {"type": "brain", "status": "active", "latency_ms": latency_ms, "source": "aion-brain"}

    @staticmethod
    def down(error: str | None, latency_ms: int | None = None) -> dict[str, Any]:
        return {"type": "brain", "status": "down", "latency_ms": latency_ms, "error": (error or "")[:200]}

    @staticmethod
    def disabled() -> dict[str, Any]:
        return {"type": "brain", "status": "disabled", "latency_ms": None}

    @staticmethod
    def skipped(reason: str) -> dict[str, Any]:
        return {"type": "brain", "status": "skipped", "latency_ms": None, "reason": reason[:200]}

brain_sse = _SseEvent()  # module-level singleton


async def decision(*, user_input: str, history: list[dict[str, Any]] | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """POST /api/decision — 7-law kernel decision for one user_input.

    Returns the dict `{request_id, decision: {...}}` from the Brain. The
    Python side may also add its own metadata (e.g. tool_context_available)
    so the Brain's decision reflects the local evidence AION has
    already collected (search results, GitHub file contents, notes, etc.).
    """
    if not is_configured():
        raise BrainUnavailable("brain_not_configured")
    body: dict[str, Any] = {"user_input": user_input}
    if history: body["history"] = history
    if metadata: body["metadata"] = metadata
    timeout = httpx.Timeout(settings.brain_timeout_ms / 1000.0)
    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{_base()}/api/decision", headers=_headers(), json=body)
    except httpx.HTTPError as exc:
        raise BrainUnavailable(f"brain_unreachable: {exc}") from exc
    latency_ms = int((time.time() - started) * 1000)
    if r.status_code == 401 or r.status_code == 403:
        audit.record("brain.decision.failed", {"error": "auth", "status": r.status_code, "latency_ms": latency_ms})
        raise BrainAuthRejected(f"brain_auth_rejected: {r.status_code}")
    if r.status_code >= 400:
        audit.record("brain.decision.failed", {"error": f"http_{r.status_code}", "latency_ms": latency_ms, "body": r.text[:200]})
        raise BrainBadResponse(f"brain_decision_failed: {r.status_code} {r.text[:200]}")
    audit.record("brain.decision.ok", {"latency_ms": latency_ms, "state": r.json().get("decision", {}).get("state")})
    return r.json()


async def stream_chat(
    *,
    messages: list[dict[str, Any]],
    temperature: float = 0.7,
    max_tokens: int = 1024,
    model: str | None = None,
    provider: str | None = None,
    session_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream events from Brain /api/chat. Yields dicts with at least a
    `type` key matching the AION v2 SSE event contract.

    If Brain is not configured, this raises BrainUnavailable before the
    first event. The caller is expected to handle that (e.g. fall back
    to the local LLM chain, or yield a single error event).
    """
    if not is_configured():
        raise BrainUnavailable("brain_not_configured")
    body: dict[str, Any] = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if model: body["model"] = model
    if provider: body["provider"] = provider
    if session_id: body["session_id"] = session_id
    timeout = httpx.Timeout(connect=10.0, read=settings.brain_timeout_ms / 1000.0, write=10.0, pool=10.0)
    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{_base()}/api/chat", headers=_headers(), json=body) as r:
                if r.status_code == 401 or r.status_code == 403:
                    audit.record("brain.chat.failed", {"error": "auth", "status": r.status_code})
                    raise BrainAuthRejected(f"brain_auth_rejected: {r.status_code}")
                if r.status_code >= 400:
                    text = (await r.aread()).decode(errors="replace")[:300]
                    audit.record("brain.chat.failed", {"error": f"http_{r.status_code}", "body": text})
                    raise BrainBadResponse(f"brain_chat_failed: {r.status_code} {text}")
                audit.record("brain.chat.started", {})
                buffer = ""
                async for chunk in r.aiter_text():
                    buffer += chunk
                    while "\n\n" in buffer:
                        block, buffer = buffer.split("\n\n", 1)
                        for line in block.split("\n"):
                            line = line.strip()
                            if not line: continue
                            if line.startswith("data:"):
                                payload = line[5:].strip()
                                if payload == "[DONE]":
                                    yield {"type": "[DONE]"}
                                    return
                                try:
                                    obj = json.loads(payload)
                                except json.JSONDecodeError:
                                    yield {"type": "error", "kind": "brain_parse_error", "raw": payload[:200]}
                                    continue
                                yield obj
                # If the stream ended without [DONE], emit one.
                yield {"type": "[DONE]"}
    except httpx.HTTPError as exc:
        raise BrainUnavailable(f"brain_unreachable: {exc}") from exc
    finally:
        audit.record("brain.chat.finished", {"latency_ms": int((time.time() - started) * 1000)})


async def run_tool(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """POST /api/tools/:name — run a kernel-level tool on Brain.

    AION can use this for lattice demos or for offloading deterministic
    tools (datetime, free_energy) that don't need the heavy LLM chain.
    """
    if not is_configured():
        raise BrainUnavailable("brain_not_configured")
    timeout = httpx.Timeout(settings.brain_timeout_ms / 1000.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{_base()}/api/tools/{name}", headers=_headers(), json=args or {})
    except httpx.HTTPError as exc:
        raise BrainUnavailable(f"brain_unreachable: {exc}") from exc
    if r.status_code == 401 or r.status_code == 403:
        raise BrainAuthRejected(f"brain_auth_rejected: {r.status_code}")
    if r.status_code == 404:
        raise BrainBadResponse(f"unknown_tool: {name}")
    if r.status_code >= 400:
        raise BrainBadResponse(f"brain_tool_failed: {r.status_code} {r.text[:200]}")
    return r.json()


async def verify_on_startup() -> dict[str, Any] | None:
    """If AION_BRAIN_STATE_CHECK is on (default), hit /api/state at
    boot to log the brain version + providers. Returns the state dict
    or None on any failure (logged, never raised)."""
    if not is_configured() or not settings.brain_state_check_on_startup:
        return None
    try:
        s = await state()
        audit.record("brain.boot_state", {
            "app": s.get("app"),
            "version": s.get("version"),
            "providers": s.get("providers"),
            "primary_model": s.get("primary_model"),
        })
        return s
    except (BrainUnavailable, BrainAuthRejected, BrainBadResponse) as exc:
        audit.record("brain.boot_state.failed", {"error": str(exc)[:200]})
        if settings.brain_required:
            raise
        return None


def require_or_warn() -> None:
    """Called at app startup. If AION_BRAIN_REQUIRED is true and the
    brain is not properly configured, raise RuntimeError so the app
    refuses to start. Otherwise log a warning and continue."""
    if not settings.brain_enabled:
        return
    if not is_configured():
        msg = "AION_BRAIN_ENABLED is true but AION_BRAIN_URL or AION_BRAIN_KEY is missing"
        if settings.brain_required:
            raise RuntimeError(msg)
        audit.record("brain.config_warning", {"reason": msg})
        return
    if settings.brain_required:
        audit.record("brain.config_ok", {"url": settings.brain_url, "timeout_ms": settings.brain_timeout_ms})
