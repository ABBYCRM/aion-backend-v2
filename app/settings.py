"""AION Settings - env-only, no pydantic-settings dependency."""
import os
class Settings:
    def __init__(self):
        self.app_name = os.environ.get("APP_NAME", "AION")
        self.app_version = os.environ.get("APP_VERSION", "1.1.0")
        self.environment = os.environ.get("ENVIRONMENT", "production")
        self.cors_origins = os.environ.get("CORS_ORIGINS", "*")
        self.log_level = os.environ.get("LOG_LEVEL", "info")
        self.openrouter_api_key = os.environ.get("OPENROUTER_API_KEY", "")
        self.openrouter_base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        self.openrouter_app_name = os.environ.get("OPENROUTER_APP_NAME", "AION-Runtime")
        self.openrouter_app_url = os.environ.get("OPENROUTER_APP_URL", "https://aion.local")
        self.moonshot_api_key = os.environ.get("MOONSHOT_API_KEY", "")
        self.moonshot_base_url = os.environ.get("MOONSHOT_BASE_URL", "https://api.moonshot.ai/v1")
        self.openai_api_key = os.environ.get("OPENAI_API_KEY", "")
        self.openai_base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.nvidia_api_key = os.environ.get("NVIDIA_API_KEY", "")
        self.nvidia_base_url = os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
        self.bitdeer_api_key = os.environ.get("BITDEER_API_KEY", "")
        self.bitdeer_base_url = os.environ.get("BITDEER_BASE_URL", "https://api-inference.bitdeer.ai/v1")
        self.cloudflare_account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        self.cloudflare_api_token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
        self.cloudflare_base_url = os.environ.get("CLOUDFLARE_BASE_URL", "")
        self.primary_model = os.environ.get("PRIMARY_MODEL", "moonshotai/kimi-k3")
        self.fallback_models = os.environ.get("FALLBACK_MODELS", "nvidia/nemotron-3-super-120b-a12b,x-ai/grok-4.5,qwen/qwen3.8-max,deepseek/deepseek-v4-pro,anthropic/claude-sonnet-5")
        self.request_timeout_seconds = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "60"))
        self.max_context_messages = int(os.environ.get("MAX_CONTEXT_MESSAGES", "40"))
        self.audit_log_dir = os.environ.get("AUDIT_LOG_DIR", "./data/audit")
    @property
    def cors_list(self):
        if self.cors_origins in ("*", ""): return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
    @property
    def model_chain(self):
        chain = [self.primary_model]
        if self.fallback_models:
            chain.extend([m.strip() for m in self.fallback_models.split(",") if m.strip()])
        seen = set(); out = []
        for m in chain:
            if m and m not in seen:
                seen.add(m); out.append(m)
        return out
    def cloudflare_url(self):
        if self.cloudflare_base_url: return self.cloudflare_base_url.rstrip("/")
        if self.cloudflare_account_id: return f"https://api.cloudflare.com/client/v4/accounts/{self.cloudflare_account_id}/ai/v1"
        return ""

settings = Settings()
