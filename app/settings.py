"""AION runtime configuration.

Secrets are supplied by the deployment environment. Production fails closed for
missing authentication, invalid CORS origins, and unsafe GitHub configuration.
Local SQLite storage is intentionally limited to development/test; production
notes require a managed PostgreSQL ``DATABASE_URL`` because App Platform local
files are ephemeral.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_PROVIDER_NAMES = {
    "openrouter",
    "moonshot",
    "openai",
    "nvidia",
    "bitdeer",
    "cloudflare",
}


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


def _validate_origin(origin: str, *, production: bool) -> None:
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"Invalid CORS origin: {origin}")
    if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise RuntimeError(f"CORS origins must be bare origins: {origin}")
    hostname = (parsed.hostname or "").lower()
    local = hostname in {"localhost", "127.0.0.1", "::1"}
    if production and parsed.scheme != "https" and not local:
        raise RuntimeError(f"Production CORS origins must use HTTPS: {origin}")


def _split_model_ref(raw: str) -> tuple[str, str]:
    value = raw.strip()
    if not value:
        return "", ""
    provider, separator, model = value.partition(":")
    if separator and provider.strip().lower() in _PROVIDER_NAMES and model.strip():
        return provider.strip().lower(), model.strip()
    return "", value


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
    max_total_message_chars: int
    max_request_bytes: int
    max_attachment_count: int
    max_image_bytes: int
    max_total_attachment_bytes: int
    max_tool_context_chars: int
    max_notes_context_chars: int
    min_completion_tokens: int
    max_completion_tokens: int
    request_timeout_seconds: int
    database_url: str
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
    primary_provider: str
    primary_model: str
    fallback_models: tuple[str, ...]
    brave_api_key: str
    brave_base_url: str
    web_search_max_results: int
    github_token: str
    allow_github_token_fallback: bool
    github_app_id: str
    github_installation_id: str
    github_private_key: str
    github_api_url: str
    github_api_version: str
    github_allowed_repositories: tuple[str, ...]
    github_write_enabled: bool

    @classmethod
    def from_env(cls) -> "Settings":
        environment = os.getenv("ENVIRONMENT", "production").strip().lower()
        development = environment != "production"
        data_dir = Path(os.getenv("AION_DATA_DIR", "./data" if development else "/tmp/aion"))
        private_key = os.getenv("GITHUB_PRIVATE_KEY", "").replace("\\n", "\n").strip()
        default_origins = "http://localhost:8000,http://localhost:5173" if development else ""
        return cls(
            app_name=os.getenv("APP_NAME", "AION").strip() or "AION",
            app_version=os.getenv("APP_VERSION", "2.1.0").strip() or "2.1.0",
            environment=environment,
            log_level=os.getenv("LOG_LEVEL", "info").strip().lower(),
            api_keys=_csv(os.getenv("AION_API_KEYS", "")),
            admin_keys=_csv(os.getenv("AION_ADMIN_KEYS", "")),
            allow_unauthenticated_dev=_bool("ALLOW_UNAUTHENTICATED_DEV", False),
            cors_origins=_csv(os.getenv("CORS_ORIGINS", default_origins)),
            rate_limit_requests=_int("RATE_LIMIT_REQUESTS", 60, minimum=1, maximum=10_000),
            rate_limit_window_seconds=_int("RATE_LIMIT_WINDOW_SECONDS", 60, minimum=1, maximum=86_400),
            max_concurrent_chats=_int("MAX_CONCURRENT_CHATS", 4, minimum=1, maximum=100),
            max_context_messages=_int("MAX_CONTEXT_MESSAGES", 40, minimum=2, maximum=200),
            max_message_chars=_int("MAX_MESSAGE_CHARS", 100_000, minimum=1_000, maximum=1_000_000),
            max_total_message_chars=_int("MAX_TOTAL_MESSAGE_CHARS", 200_000, minimum=10_000, maximum=2_000_000),
            max_request_bytes=_int("MAX_REQUEST_BYTES", 2_000_000, minimum=10_000, maximum=20_000_000),
            max_attachment_count=_int("MAX_ATTACHMENT_COUNT", 6, minimum=1, maximum=20),
            max_image_bytes=_int("MAX_IMAGE_BYTES", 900_000, minimum=10_000, maximum=5_000_000),
            max_total_attachment_bytes=_int("MAX_TOTAL_ATTACHMENT_BYTES", 1_200_000, minimum=10_000, maximum=10_000_000),
            max_tool_context_chars=_int("MAX_TOOL_CONTEXT_CHARS", 30_000, minimum=1_000, maximum=200_000),
            max_notes_context_chars=_int("MAX_NOTES_CONTEXT_CHARS", 8_000, minimum=500, maximum=50_000),
            min_completion_tokens=_int("MIN_COMPLETION_TOKENS", 32, minimum=1, maximum=4_096),
            max_completion_tokens=_int("MAX_COMPLETION_TOKENS", 4_096, minimum=64, maximum=32_768),
            request_timeout_seconds=_int("REQUEST_TIMEOUT_SECONDS", 60, minimum=5, maximum=600),
            database_url=os.getenv("DATABASE_URL", "").strip(),
            notes_db_path=os.getenv("NOTES_DB_PATH", str(data_dir / "aion.sqlite3")),
            audit_log_path=os.getenv("AUDIT_LOG_PATH", str(data_dir / "audit.jsonl") if development else "").strip(),
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
            primary_provider=os.getenv("PRIMARY_PROVIDER", "").strip().lower(),
            primary_model=os.getenv("PRIMARY_MODEL", "").strip(),
            fallback_models=_csv(os.getenv("FALLBACK_MODELS", "")),
            brave_api_key=os.getenv("BRAVE_API_KEY", "").strip(),
            brave_base_url=os.getenv("BRAVE_BASE_URL", "https://api.search.brave.com/res/v1/web/search").strip(),
            web_search_max_results=_int("WEB_SEARCH_MAX_RESULTS", 6, minimum=1, maximum=20),
            github_token=os.getenv("GITHUB_TOKEN", "").strip(),
            allow_github_token_fallback=_bool("ALLOW_GITHUB_TOKEN_FALLBACK", False),
            github_app_id=os.getenv("GITHUB_APP_ID", "").strip(),
            github_installation_id=os.getenv("GITHUB_INSTALLATION_ID", "").strip(),
            github_private_key=private_key,
            github_api_url=os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
            github_api_version=os.getenv("GITHUB_API_VERSION", "2026-03-10").strip(),
            github_allowed_repositories=tuple(item.lower() for item in _csv(os.getenv("GITHUB_ALLOWED_REPOSITORIES", ""))),
            github_write_enabled=_bool("GITHUB_WRITE_ENABLED", False),
        )

    @property
    def auth_required(self) -> bool:
        return not (self.environment != "production" and self.allow_unauthenticated_dev)

    @property
    def notes_backend(self) -> str:
        if self.database_url:
            return "postgres"
        if self.environment != "production":
            return "sqlite"
        return "disabled"

    @property
    def model_refs(self) -> list[tuple[str, str]]:
        output: list[tuple[str, str]] = []
        if self.primary_model:
            provider = self.primary_provider
            model = self.primary_model
            embedded_provider, embedded_model = _split_model_ref(self.primary_model)
            if embedded_provider:
                provider, model = embedded_provider, embedded_model
            output.append((provider, model))
        for raw in self.fallback_models:
            provider, model = _split_model_ref(raw)
            output.append((provider, model))
        seen: set[tuple[str, str]] = set()
        return [item for item in output if item[1] and not (item in seen or seen.add(item))]

    @property
    def model_chain(self) -> list[str]:
        return [f"{provider}:{model}" if provider else model for provider, model in self.model_refs]

    @property
    def cloudflare_url(self) -> str:
        if self.cloudflare_base_url:
            return self.cloudflare_base_url
        if self.cloudflare_account_id:
            return f"https://api.cloudflare.com/client/v4/accounts/{self.cloudflare_account_id}/ai/v1"
        return ""

    @property
    def github_app_configured(self) -> bool:
        return bool(self.github_app_id and self.github_installation_id and self.github_private_key)

    @property
    def github_configured(self) -> bool:
        return self.github_app_configured or bool(self.github_token and self.allow_github_token_fallback)

    def repository_allowed(self, repository: str) -> bool:
        normalized = repository.strip().lower()
        return bool(self.github_allowed_repositories) and normalized in self.github_allowed_repositories

    def validate_startup(self) -> None:
        if self.auth_required and not self.api_keys:
            raise RuntimeError("AION_API_KEYS must be configured in production")
        if self.auth_required and not self.admin_keys:
            raise RuntimeError("AION_ADMIN_KEYS must be configured in production")
        if set(self.api_keys) & set(self.admin_keys):
            raise RuntimeError("User API keys and admin API keys must be distinct")
        if self.environment == "production" and not self.cors_origins:
            raise RuntimeError("CORS_ORIGINS must contain the exact frontend origin in production")
        for origin in self.cors_origins:
            _validate_origin(origin, production=self.environment == "production")
        if self.database_url and not self.database_url.startswith(("postgres://", "postgresql://")):
            raise RuntimeError("DATABASE_URL must be a PostgreSQL URL")
        if self.primary_provider and self.primary_provider not in _PROVIDER_NAMES:
            raise RuntimeError("PRIMARY_PROVIDER is not supported")
        for provider, _ in self.model_refs:
            if provider and provider not in _PROVIDER_NAMES:
                raise RuntimeError(f"Unsupported model provider: {provider}")
            if self.environment == "production" and not provider:
                raise RuntimeError("Production model entries must use provider:model or PRIMARY_PROVIDER")
        if self.github_configured and not self.github_allowed_repositories:
            raise RuntimeError("GITHUB_ALLOWED_REPOSITORIES is required when GitHub access is configured")
        if self.github_write_enabled and not self.github_configured:
            raise RuntimeError("GitHub writes require a GitHub App or explicitly enabled token fallback")


settings = Settings.from_env()
