"""Provider-aware LLM routing with bounded, non-corrupting failover."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

try:
    from openai import AsyncOpenAI
    from openai import APIConnectionError, APIStatusError, AuthenticationError, BadRequestError, RateLimitError
except ImportError:
    AsyncOpenAI = None
    class _OpenAIUnavailableError(Exception): pass
    APIConnectionError = APIStatusError = AuthenticationError = BadRequestError = RateLimitError = _OpenAIUnavailableError

from .audit import audit
from .settings import settings


class AllProvidersFailed(RuntimeError): pass
class InvalidModelSelection(ValueError): pass
class ProviderUnavailable(RuntimeError): pass


@dataclass(frozen=True)
class ModelRef:
    provider: str
    model: str


_key_indices: dict[str, int] = {}
_catalog_cache: dict[str, Any] = {"expires": 0.0, "value": {}}
_catalog_lock = asyncio.Lock()


def _pooled_key(provider: str, raw: str) -> str:
    keys = [key.strip() for key in raw.split(",") if key.strip()]
    if not keys: return ""
    index = _key_indices.get(provider, 0) % len(keys); _key_indices[provider] = (index + 1) % len(keys); return keys[index]


def configured_providers() -> list[str]:
    providers = []
    if settings.openrouter_api_key: providers.append("openrouter")
    if settings.moonshot_api_key: providers.append("moonshot")
    if settings.openai_api_key: providers.append("openai")
    if settings.nvidia_api_key: providers.append("nvidia")
    if settings.bitdeer_api_key: providers.append("bitdeer")
    if settings.cloudflare_api_token and settings.cloudflare_url: providers.append("cloudflare")
    return providers


def _client_for(provider: str) -> Any:
    if AsyncOpenAI is None: raise ProviderUnavailable("openai_sdk_not_installed")
    if provider == "openrouter" and settings.openrouter_api_key:
        return AsyncOpenAI(api_key=settings.openrouter_api_key, base_url=settings.openrouter_base_url, timeout=settings.request_timeout_seconds, default_headers={"HTTP-Referer": settings.openrouter_app_url, "X-Title": settings.openrouter_app_name})
    if provider == "moonshot" and settings.moonshot_api_key:
        return AsyncOpenAI(api_key=settings.moonshot_api_key, base_url=settings.moonshot_base_url, timeout=settings.request_timeout_seconds)
    if provider == "openai" and settings.openai_api_key:
        return AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url, timeout=settings.request_timeout_seconds)
    if provider == "nvidia":
        key = _pooled_key("nvidia", settings.nvidia_api_key)
        if key: return AsyncOpenAI(api_key=key, base_url=settings.nvidia_base_url, timeout=settings.request_timeout_seconds)
    if provider == "bitdeer":
        key = _pooled_key("bitdeer", settings.bitdeer_api_key)
        if key: return AsyncOpenAI(api_key=key, base_url=settings.bitdeer_base_url, timeout=settings.request_timeout_seconds, default_headers={"User-Agent": f"AION/{settings.app_version}"})
    if provider == "cloudflare" and settings.cloudflare_api_token and settings.cloudflare_url:
        return AsyncOpenAI(api_key=settings.cloudflare_api_token, base_url=settings.cloudflare_url, timeout=settings.request_timeout_seconds)
    raise ProviderUnavailable(f"provider_not_configured:{provider}")


async def list_models(*, refresh: bool = False) -> dict[str, dict[str, Any]]:
    now = time.monotonic()
    if not refresh and now < float(_catalog_cache["expires"]): return _catalog_cache["value"]
    async with _catalog_lock:
        if not refresh and time.monotonic() < float(_catalog_cache["expires"]): return _catalog_cache["value"]
        output = {}
        for provider in configured_providers():
            started = time.monotonic()
            try:
                response = await _client_for(provider).models.list()
                models = sorted({str(item.id) for item in getattr(response, "data", []) if getattr(item, "id", None)})
                output[provider] = {"ok": True, "models": models, "model_count": len(models), "latency_ms": int((time.monotonic() - started) * 1000)}
            except Exception as exc:
                output[provider] = {"ok": False, "models": [], "model_count": 0, "latency_ms": int((time.monotonic() - started) * 1000), "error": _public_error(exc)}
        _catalog_cache["value"] = output; _catalog_cache["expires"] = time.monotonic() + 300; return output


async def probe() -> dict[str, dict[str, Any]]:
    catalog = await list_models(refresh=False)
    return {provider: {"ok": info.get("ok", False), "model_count": info.get("model_count", 0), "latency_ms": info.get("latency_ms", 0), **({"error": info.get("error")} if not info.get("ok") else {})} for provider, info in catalog.items()}


def _default_provider(model: str) -> str:
    if (model.startswith("moonshotai/") or model.startswith("kimi-")) and settings.moonshot_api_key: return "moonshot"
    if (model.startswith("gpt-") or model.startswith(("o1", "o3", "o4"))) and settings.openai_api_key: return "openai"
    if model.startswith(("nvidia/", "meta/", "mistralai/", "google/")) and settings.nvidia_api_key: return "nvidia"
    if model.startswith(("deepseek-", "deepseek/")) and settings.bitdeer_api_key: return "bitdeer"
    if model.startswith("@cf/") and settings.cloudflare_api_token and settings.cloudflare_url: return "cloudflare"
    return "openrouter"


async def resolve_model_chain(selected_provider: str | None, selected_model: str | None) -> list[ModelRef]:
    chain = []
    if selected_provider or selected_model:
        if not selected_provider or not selected_model: raise InvalidModelSelection("provider_and_model_are_required_together")
        if selected_provider not in configured_providers(): raise InvalidModelSelection("selected_provider_not_configured")
        known = (await list_models(refresh=False)).get(selected_provider, {}).get("models") or []
        if known and selected_model not in known: raise InvalidModelSelection("selected_model_not_available_for_provider")
        chain.append(ModelRef(selected_provider, selected_model))
    for model in settings.model_chain:
        provider = _default_provider(model)
        if provider not in configured_providers(): continue
        candidate = ModelRef(provider, model)
        if candidate not in chain: chain.append(candidate)
        if provider != "openrouter" and settings.openrouter_api_key:
            fallback = ModelRef("openrouter", model)
            if fallback not in chain: chain.append(fallback)
    if not chain: raise AllProvidersFailed("no_provider_configured")
    return chain


async def stream_chat(*, model_chain: list[ModelRef], messages: list[dict[str, Any]], temperature: float, max_tokens: int, request_id: str) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    last_error = "no_attempt"
    for index, ref in enumerate(model_chain, 1):
        emitted_output = False; attempt = {"request_id": request_id, "provider": ref.provider, "model": ref.model, "index": index}
        yield "attempt", {"provider": ref.provider, "model": ref.model, "index": index}; audit.record("llm.attempt_start", attempt); started = time.monotonic()
        try:
            stream = await _client_for(ref.provider).chat.completions.create(model=ref.model, messages=messages, temperature=temperature, max_tokens=max_tokens, stream=True)
            chars = 0; opened = False; finish_reason = None
            async for chunk in stream:
                for choice in getattr(chunk, "choices", []) or []:
                    finish_reason = getattr(choice, "finish_reason", None) or finish_reason; delta = getattr(choice, "delta", None)
                    if delta is None: continue
                    content = getattr(delta, "content", None)
                    if content:
                        if not opened: yield "open", {"provider": ref.provider, "model": ref.model}; opened = True
                        emitted_output = True; chars += len(content); yield "delta", {"text": content}
            latency = int((time.monotonic() - started) * 1000); audit.record("llm.attempt_ok", {**attempt, "latency_ms": latency, "completion_chars": chars})
            yield "done", {"provider": ref.provider, "model": ref.model, "latency_ms": latency, "completion_chars": chars, "finish_reason": finish_reason}; return
        except (AuthenticationError, RateLimitError, APIConnectionError, APIStatusError, BadRequestError, ProviderUnavailable) as exc:
            last_error = _public_error(exc); audit.record("llm.attempt_failed", {**attempt, "error_type": type(exc).__name__, "partial": emitted_output})
            if emitted_output:
                yield "error", {"kind": "partial_stream_failure", "provider": ref.provider, "model": ref.model, "message": "The selected provider disconnected after streaming began. The partial response was not mixed with another model."}; return
            yield "error", {"kind": _error_kind(exc), "provider": ref.provider, "model": ref.model, "message": last_error}; continue
        except Exception as exc:
            last_error = "provider_unexpected_error"; audit.record("llm.attempt_unexpected", {**attempt, "error_type": type(exc).__name__, "partial": emitted_output})
            if emitted_output:
                yield "error", {"kind": "partial_stream_failure", "provider": ref.provider, "model": ref.model, "message": "The provider failed after streaming began. The partial response was preserved without cross-model concatenation."}; return
    raise AllProvidersFailed(last_error)


def _error_kind(exc: Exception) -> str:
    if isinstance(exc, AuthenticationError): return "authentication"
    if isinstance(exc, RateLimitError): return "rate_limit"
    if isinstance(exc, BadRequestError): return "bad_request"
    if isinstance(exc, ProviderUnavailable): return "provider_unavailable"
    return "transport"


def _public_error(exc: Exception) -> str:
    if isinstance(exc, AuthenticationError): return "provider_authentication_failed"
    if isinstance(exc, RateLimitError): return "provider_rate_limited"
    if isinstance(exc, BadRequestError): return "provider_rejected_request"
    if isinstance(exc, ProviderUnavailable): return str(exc)
    if isinstance(exc, (APIConnectionError, APIStatusError)): return "provider_unavailable"
    return "provider_error"
