"""Provider-aware LLM routing with bounded, non-corrupting failover."""
from __future__ import annotations

import asyncio
import os
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

# Lazy import to avoid circular: vault reads settings which is imported above
def _vault_value(name: str) -> str:
    """Read a secret from the vault, falling back to settings/env. The
    vault is the single source of truth at runtime once the operator
    has rotated a key via /api/vault/{name}/rotate."""
    try:
        from .vault import vault as _vault
        v = _vault.reveal(name)
        if v: return v
    except Exception:
        pass
    return os.getenv(name, "").strip()


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
    if _vault_value("OPENAI_API_KEY"): providers.append("openai")
    if _vault_value("MOONSHOT_API_KEY") or _vault_value("KIMI_API_KEY"): providers.append("moonshot")
    if _vault_value("NVIDIA_API_KEY"): providers.append("nvidia")
    if _vault_value("XAI_API_KEY"): providers.append("xai")
    if _vault_value("BITDEER_API_KEY"): providers.append("bitdeer")
    if _vault_value("OPENROUTER_API_KEY"): providers.append("openrouter")
    if _vault_value("ANTHROPIC_API_KEY"): providers.append("anthropic")
    if _vault_value("HELICONE_API_KEY"): providers.append("helicone")
    if _vault_value("CLOUDFLARE_API_TOKEN") and _vault_value("CLOUDFLARE_BASE_URL"): providers.append("cloudflare")
    return providers


def _client_for(provider: str) -> Any:
    if AsyncOpenAI is None: raise ProviderUnavailable("openai_sdk_not_installed")
    if provider == "openai":
        key = _vault_value("OPENAI_API_KEY")
        if key: return AsyncOpenAI(api_key=key, base_url=settings.openai_base_url, timeout=settings.request_timeout_seconds)
    if provider == "moonshot":
        key = _vault_value("MOONSHOT_API_KEY") or _vault_value("KIMI_API_KEY")
        if key:
            base = settings.moonshot_base_url
            # Moonshot / Kimi uses different base URLs depending on region.
            if "moonshot.cn" in base or not base:
                base = "https://api.moonshot.cn/v1"
            return AsyncOpenAI(api_key=key, base_url=base, timeout=settings.request_timeout_seconds)
    if provider == "nvidia":
        key = _pooled_key("nvidia", _vault_value("NVIDIA_API_KEY"))
        if key: return AsyncOpenAI(api_key=key, base_url=settings.nvidia_base_url, timeout=settings.request_timeout_seconds)
    if provider == "xai":
        key = _pooled_key("xai", _vault_value("XAI_API_KEY"))
        if key: return AsyncOpenAI(api_key=key, base_url=settings.xai_base_url, timeout=settings.request_timeout_seconds)
    if provider == "bitdeer":
        key = _pooled_key("bitdeer", _vault_value("BITDEER_API_KEY"))
        if key: return AsyncOpenAI(api_key=key, base_url=settings.bitdeer_base_url, timeout=settings.request_timeout_seconds, default_headers={"User-Agent": f"AION/{settings.app_version}"})
    if provider == "openrouter":
        key = _vault_value("OPENROUTER_API_KEY")
        if key:
            return AsyncOpenAI(api_key=key, base_url=settings.openrouter_base_url, timeout=settings.request_timeout_seconds, default_headers={"HTTP-Referer": settings.openrouter_app_url, "X-Title": settings.openrouter_app_name})
    if provider == "anthropic":
        # Anthropic is NOT OpenAI-compatible; we route through the OpenAI
        # client only if a base URL is set that exposes a compat shim.
        # The default path is /v1/messages and uses x-api-key + anthropic-version.
        # For now, expose it via a thin adapter: if OPENAI_BASE_URL is pointed
        # at an Anthropic-compat proxy, use that. Otherwise this provider is
        # not configured.
        key = _vault_value("ANTHROPIC_API_KEY")
        if key and settings.openai_base_url and "anthropic" in settings.openai_base_url.lower():
            return AsyncOpenAI(api_key=key, base_url=settings.openai_base_url, timeout=settings.request_timeout_seconds)
    if provider == "helicone":
        # Helicone is a proxy: hit api.helicone.ai as OpenAI-compatible with
        # the user's OpenAI key passed via Authorization and the Helicone
        # key in the Helicone-Auth header.
        hkey = _vault_value("HELICONE_API_KEY")
        okey = _vault_value("OPENAI_API_KEY")
        if hkey and okey:
            return AsyncOpenAI(
                api_key=okey,
                base_url="https://oai.helicone.ai/v1",
                timeout=settings.request_timeout_seconds,
                default_headers={"Helicone-Auth": f"Bearer {hkey}"},
            )
    if provider == "cloudflare":
        key = _vault_value("CLOUDFLARE_API_TOKEN")
        url = _vault_value("CLOUDFLARE_BASE_URL")
        if key and url:
            return AsyncOpenAI(api_key=key, base_url=url, timeout=settings.request_timeout_seconds)
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
    if (model.startswith("moonshotai/") or model.startswith("kimi-")) and (_vault_value("MOONSHOT_API_KEY") or _vault_value("KIMI_API_KEY")): return "moonshot"
    if (model.startswith("gpt-") or model.startswith(("o1", "o3", "o4"))) and _vault_value("OPENAI_API_KEY"):
        if _vault_value("HELICONE_API_KEY"): return "helicone"
        return "openai"
    if model.startswith(("nvidia/", "meta/", "mistralai/", "google/")) and _vault_value("NVIDIA_API_KEY"): return "nvidia"
    if (model.startswith("xai/") or model.startswith("grok-")) and _vault_value("XAI_API_KEY"): return "xai"
    if model.startswith(("deepseek-", "deepseek/")) and _vault_value("BITDEER_API_KEY"): return "bitdeer"
    if model.startswith("@cf/") and _vault_value("CLOUDFLARE_API_TOKEN") and _vault_value("CLOUDFLARE_BASE_URL"): return "cloudflare"
    if model.startswith("claude-") and _vault_value("ANTHROPIC_API_KEY"): return "anthropic"
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


async def complete_chat(*, provider: str, model: str, messages: list[dict[str, Any]], temperature: float, max_tokens: int, request_id: str = "mirror") -> tuple[str, int]:
    """Non-streaming chat call. Returns (text, tokens_used). Used by the
    answer mirror for short audit and revision calls."""
    try:
        client = _client_for(provider)
    except ProviderUnavailable as exc:
        raise AllProvidersFailed(str(exc)) from exc
    started = time.monotonic()
    try:
        response = await client.chat.completions.create(
            model=model, messages=messages, temperature=temperature, max_tokens=max_tokens, stream=False,
        )
    except (AuthenticationError, RateLimitError, APIConnectionError, APIStatusError, BadRequestError) as exc:
        audit.record("llm.attempt_failed", {"request_id": request_id, "provider": provider, "model": model, "error_type": type(exc).__name__})
        raise AllProvidersFailed(_public_error(exc)) from exc
    latency = int((time.monotonic() - started) * 1000)
    text = ""
    if response.choices:
        text = getattr(response.choices[0].message, "content", "") or ""
    tokens = 0
    if getattr(response, "usage", None):
        tokens = getattr(response.usage, "total_tokens", 0) or 0
    audit.record("llm.attempt_ok", {"request_id": request_id, "provider": provider, "model": model, "latency_ms": latency, "completion_chars": len(text)})
    return text, tokens


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
