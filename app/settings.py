"""AION runtime configuration.

All secrets are supplied by the deployment environment. Production fails closed
when no API keys are configured.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        range_text = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise RuntimeError(f"{name} must be {range_text}")
    return value


@dataclass(frozen=True)
class Settings:
    app_name: str
    app_version: str
    environment: str
    log_level: str
    api_keys: tuple[str, ...]
    admin_keys: tuple[str, ...]
    allow_unauthenticated_dev: bool
    cors_origins: tuple[str, ...]
    rate_limit_requests: int
    rate_limit_window_seconds: int
    max_concurrent_chats: int
    max_context_messages: int
    max_message_chars: int
    max_request_bytes: int
    min_completion_tokens: int
    max_completion_tokens: int
    request_timeout_seconds: int
    notes_db_path: str
    audit_log_path: str
    audit_retention_lines: int
    openrouter_api_key: str
    openrouter_base_url: str
    openrouter_app_name: str
    openrouter_app_url: str
    moonshot_api_key: str
    moonshot_base_url: str
    openai_api_key: str
    openai_base_url: str
    nvidia_api_key: str
    nvidia_base_url: str
    bitdeer_api_key: str
    bitdeer_base_url: str
    cloudflare_account_id: str
    cloudflare_api_token: str
    cloudflare_base_url: str
    primary_model: str
    fallback_models: tuple[str, ...]
    brave_api_key: str
    brave_base_url: str
    web_search_max_results: int
    github_token: str
    github_app_id: str
    github_installation_id: str
    github_private_key: str
    github_api_url: str
    github_allowed_repositories: tuple[str, ...]
    github_write_enabled: bool
    # Aion-Brain integration. When brain_enabled is true and brain_url is set,
    # AION Python delegates decision + chat to the Brain kernel.
    brain_enabled: bool
    brain_url: str
    brain_service_key: str
    brain_timeout_ms: int
    brain_decision_only: bool
    brain_required: bool
    brain_state_check_on_startup: bool
    # Reflector (answer mirror). Off by default — opt-in via settings.
    reflector_enabled: bool
    reflector_auditor_model: str

    @classmethod
    def from_env(cls) -> "Settings":
        environment = os.getenv("ENVIRONMENT", "production").strip().lower()
        data_dir = Path(os.getenv("AION_DATA_DIR", "/var/data" if environment == "production" else "./data"))
        private_key = os.getenv("GITHUB_PRIVATE_KEY", "").replace("\\n", "\n").strip()
        return cls(
            app_name=os.getenv("APP_NAME", "AION").strip() or "AION",
            app_version=os.getenv("APP_VERSION", "2.8.12").strip() or "2.0.0",
            environment=environment,
            log_level=os.getenv("LOG_LEVEL", "info").strip().lower(),
            api_keys=_csv(os.getenv("AION_API_KEYS", "")),
            admin_keys=_csv(os.getenv("AION_ADMIN_KEYS", "")),
            allow_unauthenticated_dev=_bool("ALLOW_UNAUTHENTICATED_DEV", False),
            cors_origins=_csv(os.getenv("CORS_ORIGINS", "http://localhost:8000,http://localhost:5173")),
            rate_limit_requests=_int("RATE_LIMIT_REQUESTS", 60, minimum=1, maximum=10000),
            rate_limit_window_seconds=_int("RATE_LIMIT_WINDOW_SECONDS", 60, minimum=1, maximum=86400),
            max_concurrent_chats=_int("MAX_CONCURRENT_CHATS", 4, minimum=1, maximum=100),
            max_context_messages=_int("MAX_CONTEXT_MESSAGES", 40, minimum=2, maximum=200),
            max_message_chars=_int("MAX_MESSAGE_CHARS", 100_000, minimum=1000, maximum=1_000_000),
            max_request_bytes=_int("MAX_REQUEST_BYTES", 2_000_000, minimum=10_000, maximum=20_000_000),
            min_completion_tokens=_int("MIN_COMPLETION_TOKENS", 32, minimum=1, maximum=4096),
            max_completion_tokens=_int("MAX_COMPLETION_TOKENS", 4096, minimum=64, maximum=32768),
            request_timeout_seconds=_int("REQUEST_TIMEOUT_SECONDS", 60, minimum=5, maximum=600),
            notes_db_path=os.getenv("NOTES_DB_PATH", str(data_dir / "aion.sqlite3")),
            audit_log_path=os.getenv("AUDIT_LOG_PATH", str(data_dir / "audit.jsonl")),
            audit_retention_lines=_int("AUDIT_RETENTION_LINES", 10_000, minimum=100, maximum=1_000_000),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
            openrouter_app_name=os.getenv("OPENROUTER_APP_NAME", "AION-Runtime").strip(),
            openrouter_app_url=os.getenv("OPENROUTER_APP_URL", "https://aion.local").strip(),
            moonshot_api_key=os.getenv("MOONSHOT_API_KEY", "").strip(),
            moonshot_base_url=os.getenv("MOONSHOT_BASE_URL", "https://api.moonshot.ai/v1").rstrip("/"),
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            nvidia_api_key=os.getenv("NVIDIA_API_KEY", "").strip(),
            nvidia_base_url=os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/"),
            bitdeer_api_key=os.getenv("BITDEER_API_KEY", "").strip(),
            bitdeer_base_url=os.getenv("BITDEER_BASE_URL", "https://api-inference.bitdeer.ai/v1").rstrip("/"),
            cloudflare_account_id=os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip(),
            cloudflare_api_token=os.getenv("CLOUDFLARE_API_TOKEN", "").strip(),
            cloudflare_base_url=os.getenv("CLOUDFLARE_BASE_URL", "").rstrip("/"),
            primary_model=os.getenv("PRIMARY_MODEL", "moonshotai/kimi-k3").strip(),
            fallback_models=_csv(os.getenv("FALLBACK_MODELS", "nvidia/nemotron-3-super-120b-a12b,deepseek/deepseek-chat")),
            brave_api_key=os.getenv("BRAVE_API_KEY", "").strip(),
            brave_base_url=os.getenv("BRAVE_BASE_URL", "https://api.search.brave.com/res/v1/web/search").strip(),
            web_search_max_results=_int("WEB_SEARCH_MAX_RESULTS", 12, minimum=1, maximum=20),
            github_token=os.getenv("GITHUB_TOKEN", "").strip(),
            github_app_id=os.getenv("GITHUB_APP_ID", "").strip(),
            github_installation_id=os.getenv("GITHUB_INSTALLATION_ID", "").strip(),
            github_private_key=private_key,
            github_api_url=os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
            github_allowed_repositories=tuple(item.lower() for item in _csv(os.getenv("GITHUB_ALLOWED_REPOSITORIES", ""))),
            github_write_enabled=_bool("GITHUB_WRITE_ENABLED", False),
            brain_enabled=_bool("AION_BRAIN_ENABLED", False),
            brain_url=os.getenv("AION_BRAIN_URL", "").strip().rstrip("/"),
            brain_service_key=os.getenv("AION_BRAIN_KEY", "").strip(),
            brain_timeout_ms=_int("AION_BRAIN_TIMEOUT_MS", 60_000, minimum=1_000, maximum=600_000),
            brain_decision_only=_bool("AION_BRAIN_DECISION_ONLY", False),
            brain_required=_bool("AION_BRAIN_REQUIRED", False),
            brain_state_check_on_startup=_bool("AION_BRAIN_STATE_CHECK", True),
            reflector_enabled=_bool("AION_REFLECTOR_ENABLED", False),
            reflector_auditor_model=os.getenv("AION_REFLECTOR_AUDITOR_MODEL", "").strip(),
        )

    @property
    def auth_required(self) -> bool:
        return not (self.environment != "production" and self.allow_unauthenticated_dev)

    @property
    def model_chain(self) -> list[str]:
        seen: set[str] = set()
        output: list[str] = []
        for model in (self.primary_model, *self.fallback_models):
            if model and model not in seen:
                seen.add(model)
                output.append(model)
        return output

    @property
    def cloudflare_url(self) -> str:
        if self.cloudflare_base_url:
            return self.cloudflare_base_url
        if self.cloudflare_account_id:
            return f"https://api.cloudflare.com/client/v4/accounts/{self.cloudflare_account_id}/ai/v1"
        return ""

    def repository_allowed(self, repository: str) -> bool:
        normalized = repository.strip().lower()
        return not self.github_allowed_repositories or normalized in self.github_allowed_repositories

    def validate_startup(self) -> None:
        if self.auth_required and not self.api_keys:
            raise RuntimeError("AION_API_KEYS must be configured in production")
        if self.auth_required and not self.admin_keys:
            raise RuntimeError("AION_ADMIN_KEYS must be configured in production")
        overlap = set(self.api_keys) & set(self.admin_keys)
        if overlap:
            raise RuntimeError("User API keys and admin API keys must be distinct")
        if self.github_write_enabled and not (self.github_token or self.github_app_configured):
            raise RuntimeError("GitHub writes require a GitHub App or server-side token")

    @property
    def github_app_configured(self) -> bool:
        return bool(self.github_app_id and self.github_installation_id and self.github_private_key)


settings = Settings.from_env()
