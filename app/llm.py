"""
AION LLM Router — provider-agnostic with live failover.

Priority order:
  1. OpenRouter (gives access to Kimi, Grok, Qwen, DeepSeek, Claude, Gemini)
  2. Moonshot direct (api.moonshot.ai/v1) if key present
  3. OpenAI direct (api.openai.com/v1) if key present

Each call:
  - picks the first healthy model in the chain
  - streams tokens via OpenAI-compatible SDK
  - on 401/403/429/5xx tries the next model
  - records every attempt to the audit log
"""
from __future__ import annotations
import time
from dataclasses import dataclass
from typing import AsyncIterator, Dict, List, Optional, Tuple

from openai import AsyncOpenAI
from openai import (
    APIConnectionError,
    AuthenticationError,
    RateLimitError,
    BadRequestError,
    APIStatusError,
)

from .audit import audit
from .settings import settings


# ---------------------------------------------------------------------------
# Key pool — round-robin across comma-separated keys.
# Used for providers with tight rate limits (NVIDIA NIM, Bitdeer).
# ---------------------------------------------------------------------------
_pool_state: Dict[str, dict] = {}


def _key_pool(provider: str) -> Optional[str]:
    """Return the next key in round-robin order, or None if not configured."""
    raw = ""
    if provider == "nvidia":
        raw = settings.nvidia_api_key
    elif provider == "bitdeer":
        raw = settings.bitdeer_api_key
    else:
        # OpenRouter / Moonshot / OpenAI / Cloudflare — single key path
        return None
    keys = [k.strip() for k in (raw or "").split(",") if k.strip()]
    if not keys:
        return None
    if provider not in _pool_state:
        _pool_state[provider] = {"idx": 0}
    state_ = _pool_state[provider]
    key = keys[state_["idx"] % len(keys)]
    state_["idx"] = (state_["idx"] + 1) % len(keys)
    return key


@dataclass
class LLMAttempt:
    provider: str
    model: str
    base_url: str
    success: bool
    error: Optional[str] = None
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


def _client_for(provider: str) -> Optional[Tuple[AsyncOpenAI, str]]:
    if provider == "openrouter":
        if not settings.openrouter_api_key:
            return None
        return (
            AsyncOpenAI(
                api_key=settings.openrouter_api_key,
                base_url=settings.openrouter_base_url,
                default_headers={
                    "HTTP-Referer": settings.openrouter_app_url,
                    "X-Title": settings.openrouter_app_name,
                },
                timeout=settings.request_timeout_seconds,
            ),
            settings.openrouter_base_url,
        )
    if provider == "moonshot":
        if not settings.moonshot_api_key:
            return None
        return (
            AsyncOpenAI(
                api_key=settings.moonshot_api_key,
                base_url=settings.moonshot_base_url,
                timeout=settings.request_timeout_seconds,
            ),
            settings.moonshot_base_url,
        )
    if provider == "openai":
        if not settings.openai_api_key:
            return None
        return (
            AsyncOpenAI(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                timeout=settings.request_timeout_seconds,
            ),
            settings.openai_base_url,
        )
    if provider == "nvidia":
        key = _key_pool("nvidia")
        if not key:
            return None
        return (
            AsyncOpenAI(
                api_key=key,
                base_url=settings.nvidia_base_url,
                timeout=settings.request_timeout_seconds,
            ),
            settings.nvidia_base_url,
        )
    if provider == "bitdeer":
        key = _key_pool("bitdeer")
        if not key:
            return None
        return (
            AsyncOpenAI(
                api_key=key,
                base_url=settings.bitdeer_base_url,
                # Bitdeer AI Inference requires a real User-Agent or it
                # gets 403'd by Cloudflare edge. The SDK doesn't set one
                # by default; AsyncOpenAI accepts default_headers.
                default_headers={"User-Agent": "AION-Runtime/1.1"},
                timeout=settings.request_timeout_seconds,
            ),
            settings.bitdeer_base_url,
        )
    if provider == "cloudflare":
        url = settings.cloudflare_url()
        if not settings.cloudflare_api_token or not url:
            return None
        return (
            AsyncOpenAI(
                api_key=settings.cloudflare_api_token,
                base_url=url,
                timeout=settings.request_timeout_seconds,
            ),
            url,
        )
    return None


def _provider_for_model(model: str) -> str:
    """Route a model id to its provider. OpenRouter is the default for
    cross-vendor model ids; direct providers only handle their own model ids
    when a key is configured."""
    # NVIDIA NIM model ids follow `org/model`; the *direct* NVIDIA endpoint
    # accepts the same ids but needs a real nvapi key.
    if model.startswith("nvidia/") or model.startswith("meta/") or model.startswith("mistralai/") or model.startswith("google/"):
        return "nvidia" if settings.nvidia_api_key else "openrouter"
    # Bitdeer serves DeepSeek/Qwen/Llama; we route those to bitdeer when key present.
    if settings.bitdeer_api_key and model.startswith("deepseek-"):
        return "bitdeer"
    if model.startswith("moonshotai/") or model.startswith("kimi-"):
        return "moonshot" if settings.moonshot_api_key else "openrouter"
    if model.startswith("gpt-") or model.startswith("o1") or model.startswith("o3"):
        return "openai" if settings.openai_api_key else "openrouter"
    # Cloudflare Workers AI model ids are bare like @cf/meta/llama-3.1-8b-instruct
    if model.startswith("@cf/"):
        return "cloudflare" if (settings.cloudflare_api_token and settings.cloudflare_url()) else "openrouter"
    return "openrouter"


def _available_providers() -> List[str]:
    out = []
    if settings.openrouter_api_key:
        out.append("openrouter")
    if settings.moonshot_api_key:
        out.append("moonshot")
    if settings.openai_api_key:
        out.append("openai")
    if settings.nvidia_api_key:
        out.append("nvidia")
    if settings.bitdeer_api_key:
        out.append("bitdeer")
    if settings.cloudflare_api_token and settings.cloudflare_url():
        out.append("cloudflare")
    return out


# ---------------------------------------------------------------------------
# Health probe — at startup, hit /v1/models to confirm key works.
# ---------------------------------------------------------------------------
async def probe() -> Dict[str, dict]:
    """Return health info for every provider/model we might call."""
    results: Dict[str, dict] = {}
    for provider in _available_providers():
        c = _client_for(provider)
        if not c:
            continue
        client, base = c
        t0 = time.time()
        try:
            r = await client.models.list()
            ids = sorted({m.id for m in getattr(r, "data", [])})
            results[provider] = {
                "ok": True,
                "base": base,
                "model_count": len(ids),
                "latency_ms": int((time.time() - t0) * 1000),
            }
        except Exception as e:
            results[provider] = {
                "ok": False,
                "base": base,
                "error": f"{type(e).__name__}: {e}",
                "latency_ms": int((time.time() - t0) * 1000),
            }
    return results


# ---------------------------------------------------------------------------
# Streaming chat completion with model-chain failover.
# ---------------------------------------------------------------------------
class AllProvidersFailed(Exception):
    pass


async def _try_one_attempt(
    *,
    provider: str,
    model: str,
    messages: List[dict],
    temperature: float,
    max_tokens: int,
    request_id: str,
    attempt_idx: int,
):
    """Yield events for a single (provider, model) attempt. Raises on
    transport failure. Returns nothing on success."""
    c = _client_for(provider)
    if not c:
        return
    client, base = c
    attempt_started = time.time()
    attempt_event = {
        "request_id": request_id,
        "provider": provider,
        "model": model,
        "base_url": base,
        "index": attempt_idx,
    }
    yield ("attempt", attempt_event)
    audit.record("llm.attempt_start", attempt_event)

    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
        stream_options={"include_usage": True},
    )

    opened = False
    content_buf = ""
    usage_payload: dict = {}

    async for chunk in stream:
        if getattr(chunk, "usage", None):
            u = chunk.usage
            usage_payload = {
                "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
                "total_tokens": getattr(u, "total_tokens", 0) or 0,
            }
        for choice in getattr(chunk, "choices", []) or []:
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            content = getattr(delta, "content", None)
            if content:
                if not opened:
                    yield ("open", {"model": model, "provider": provider})
                    opened = True
                content_buf += content
                yield ("delta", {"text": content})

    latency = int((time.time() - attempt_started) * 1000)
    audit.record("llm.attempt_ok", {
        **attempt_event,
        "latency_ms": latency,
        "completion_chars": len(content_buf),
        "usage": usage_payload,
    })
    yield ("done", {
        "model": model,
        "provider": provider,
        "latency_ms": latency,
        "completion_chars": len(content_buf),
        "usage": usage_payload,
    })


async def stream_chat(
    *,
    model_chain: List[str],
    messages: List[dict],
    temperature: float = 0.7,
    max_tokens: int = 2048,
    request_id: str = "",
) -> AsyncIterator[Tuple[str, dict]]:
    """
    Yield (event_type, payload) tuples over the wire.
    event_type ∈ {open, delta, done, error, attempt}.
    """
    last_error: Optional[str] = None
    attempt_idx = 0
    for model in model_chain:
        # Build the candidate provider list for this model:
        # primary provider first, then OpenRouter as a safety net.
        primary = _provider_for_model(model)
        candidates = [primary]
        if primary != "openrouter" and settings.openrouter_api_key:
            candidates.append("openrouter")

        for provider in candidates:
            attempt_idx += 1
            try:
                gen = _try_one_attempt(
                    provider=provider,
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    request_id=request_id,
                    attempt_idx=attempt_idx,
                )
                async for ev in gen:
                    yield ev
                return
            except AuthenticationError as e:
                err = f"auth_failed: {e}"
                last_error = err
                audit.record("llm.attempt_auth_fail", {"request_id": request_id, "model": model, "provider": provider, "error": err})
                yield ("error", {"model": model, "provider": provider, "kind": "auth", "message": str(e)})
                # Don't try other providers with the same bad key
                if provider == primary:
                    break
                continue
            except RateLimitError as e:
                err = f"rate_limited: {e}"
                last_error = err
                audit.record("llm.attempt_rate_limit", {"request_id": request_id, "model": model, "provider": provider, "error": err})
                yield ("error", {"model": model, "provider": provider, "kind": "rate_limit", "message": str(e)})
                continue
            except (APIConnectionError, APIStatusError, BadRequestError) as e:
                err = f"{type(e).__name__}: {e}"
                last_error = err
                audit.record("llm.attempt_transport_fail", {"request_id": request_id, "model": model, "provider": provider, "error": err})
                yield ("error", {"model": model, "provider": provider, "kind": "transport", "message": err})
                continue
            except Exception as e:
                err = f"unexpected: {type(e).__name__}: {e}"
                last_error = err
                audit.record("llm.attempt_unexpected", {"request_id": request_id, "model": model, "provider": provider, "error": err})
                yield ("error", {"model": model, "provider": provider, "kind": "unexpected", "message": err})
                continue

    raise AllProvidersFailed(last_error or "no provider available")
