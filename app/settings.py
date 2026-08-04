"""
AION Runtime — Settings
Provider-agnostic. Reads from env only. Never hardcode keys.
"""
from __future__ import annotations
import os
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---
    app_name: str = "AION"
    app_version: str = "1.1.0"
    environment: str = "production"
    cors_origins: str = "*"
    log_level: str = "info"

    # --- LLM Providers (priority order) ---
    # OpenRouter key gives access to Kimi, Grok, Qwen, DeepSeek, Claude, Gemini.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Direct providers (optional fallbacks)
    moonshot_api_key: str = ""      # api.moonshot.ai/v1
    moonshot_base_url: str = "https://api.moonshot.ai/v1"
    openai_api_key: str = ""        # api.openai.com/v1
    openai_base_url: str = "https://api.openai.com/v1"

    # NVIDIA NIM (integrate.api.nvidia.com) — nvapi-... key
    # Supports a comma-separated pool for round-robin rate-limit distribution.
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"

    # Bitdeer AI Cloud Inference (https://www.bitdeer.ai/en/docs/inference/)
    # Their docs confirm the real OpenAI-compatible base URL is
    # https://api-inference.bitdeer.ai/v1
    # Bitdeer edge requires a real User-Agent or Cloudflare 403s the request.
    bitdeer_api_key: str = ""
    bitdeer_base_url: str = "https://api-inference.bitdeer.ai/v1"

    # Cloudflare Workers AI (per-account) — uses Account ID + API token
    cloudflare_account_id: str = ""
    cloudflare_api_token: str = ""
    cloudflare_base_url: str = ""  # auto-built when account_id is set

    # Model chain (first available wins; live-tested at startup)
    primary_model: str = "moonshotai/kimi-k3"
    # OpenRouter routed; if NVIDIA/Bitdeer/Cloudflare direct keys are set, those
    # providers are inserted into the chain automatically before the OpenRouter
    # models (direct provider takes precedence over routed equivalent).
    fallback_models: str = (
        "nvidia/nemotron-3-super-120b-a12b,"
        "x-ai/grok-4.5,"
        "qwen/qwen3.8-max,"
        "deepseek/deepseek-v4-pro,"
        "anthropic/claude-sonnet-5"
    )

    # OpenRouter app attribution (required for some models)
    openrouter_app_name: str = "AION-Runtime"
    openrouter_app_url: str = "https://aion.local"

    # --- Runtime knobs ---
    request_timeout_seconds: int = 60
    max_context_messages: int = 40
    audit_log_dir: str = "./data/audit"

    @property
    def cors_list(self) -> List[str]:
        raw = (self.cors_origins or "").strip()
        if raw == "*" or raw == "":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]

    @property
    def model_chain(self) -> List[str]:
        chain = [self.primary_model]
        if self.fallback_models:
            chain.extend([m.strip() for m in self.fallback_models.split(",") if m.strip()])
        # de-dup preserve order
        seen = set()
        out = []
        for m in chain:
            if m and m not in seen:
                seen.add(m)
                out.append(m)
        return out

    def cloudflare_url(self) -> str:
        if self.cloudflare_base_url:
            return self.cloudflare_base_url.rstrip("/")
        if self.cloudflare_account_id:
            return f"https://api.cloudflare.com/client/v4/accounts/{self.cloudflare_account_id}/ai/v1"
        return ""


settings = Settings()
