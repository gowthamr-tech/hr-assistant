"""Runtime configuration, read once from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import Optional

MAX_CORPUS_TOKENS = 3000


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value.strip() if value is not None and value.strip() != "" else default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "")
    if raw == "":
        return default
    if raw.lower() in {"1", "true", "yes", "on"}:
        return True
    if raw.lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false, got {raw!r}")


def _env_num(name: str, default: str, cast=float, minimum: float = 0):
    raw = _env(name, default)
    try:
        value = cast(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {raw!r}")
    return value


def _env_list(name: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in _env(name).split(",") if v.strip())


AUTH_MODES = ("header", "gateway", "jwt")


@dataclass(frozen=True)
class Settings:
    corpus_dir: str
    manifest_path: str
    db_path: str
    app_today: Optional[date]
    llm_provider: str
    llm_model: str
    llm_router_model: str
    llm_timeout_s: float
    llm_max_retries: int
    anthropic_effort: str
    anthropic_refusal_fallback: bool
    embeddings_enabled: bool
    embedding_model: str
    log_level: str
    log_file: str
    request_deadline_s: float
    max_corpus_tokens: int = MAX_CORPUS_TOKENS
    api_keys: dict = field(default_factory=dict)
    ollama_base_url: str = "http://localhost:11434/v1"
    log_format: str = "auto"
    # ---- production settings (defaults keep the original development behaviour)
    app_env: str = "development"            # development | production
    auth_mode: str = "header"               # header | gateway | jwt (see app/security/auth.py)
    gateway_token: str = ""
    jwt_secret: str = ""                    # HS* algorithms
    jwt_public_key: str = ""                # RS*/ES* algorithms (PEM)
    jwt_algorithms: tuple = ("HS256",)
    jwt_issuer: str = ""
    jwt_audience: str = ""
    jwt_employee_claim: str = "sub"
    rate_limit_per_minute: int = 30         # per employee; 0 disables
    cors_allow_origins: tuple = ()
    ui_enabled: bool = True
    metrics_enabled: bool = True
    retention_days: int = 90                # chat history and sessions; 0 keeps forever
    pending_ttl_minutes: int = 30           # an unconfirmed leave proposal expires after this

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def today(self) -> date:
        return self.app_today or date.today()


_DEFAULT_MODELS = {
    "anthropic": "claude-opus-5-5",
    "openai": "gpt-4.1-mini",
    "gemini": "gemini-3-flash-preview",
    "google": "gemini-3-flash-preview",
    "ollama": "qwen2.5:7b",
}


def load_settings() -> Settings:
    today_raw = _env("APP_TODAY")
    app_today = None
    if today_raw:
        try:
            app_today = date.fromisoformat(today_raw)
        except ValueError as exc:  # fail fast: a wrong "today" corrupts every date rule
            raise ValueError(f"APP_TODAY must be YYYY-MM-DD, got {today_raw!r}") from exc

    provider = _env("LLM_PROVIDER", "anthropic").lower()
    if provider not in _DEFAULT_MODELS and provider != "fake":
        raise ValueError(f"Unsupported LLM_PROVIDER {provider!r}; use anthropic, openai, gemini or ollama")
    model = _env("LLM_MODEL", _DEFAULT_MODELS.get(provider, ""))

    app_env = _env("APP_ENV", "development").lower()
    if app_env not in ("development", "production"):
        raise ValueError(f"APP_ENV must be development or production, got {app_env!r}")
    production = app_env == "production"

    settings = Settings(
        corpus_dir=_env("CORPUS_DIR", "./data/corpus"),
        manifest_path=_env("MANIFEST_PATH", "./data/manifest.json"),
        db_path=_env("DB_PATH", "./data/veltrix.db"),
        app_today=app_today,
        llm_provider=provider,
        llm_model=model,
        llm_router_model=_env("LLM_ROUTER_MODEL", model),
        llm_timeout_s=_env_num("LLM_TIMEOUT_S", "20", minimum=1),
        llm_max_retries=_env_num("LLM_MAX_RETRIES", "1", int),
        anthropic_effort=_env("ANTHROPIC_EFFORT", "low"),
        anthropic_refusal_fallback=_env_bool("ANTHROPIC_REFUSAL_FALLBACK", True),
        embeddings_enabled=_env_bool("EMBEDDINGS_ENABLED", True),
        embedding_model=_env("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
        log_level=_env("LOG_LEVEL", "INFO"),
        log_file=_env("LOG_FILE", ""),
        log_format=_env("LOG_FORMAT", "auto"),
        request_deadline_s=_env_num("REQUEST_DEADLINE_S", "27", minimum=1),
        api_keys={
            "anthropic": _env("ANTHROPIC_API_KEY"),
            "openai": _env("OPENAI_API_KEY"),
            "gemini": _env("GOOGLE_API_KEY") or _env("GEMINI_API_KEY"),
        },
        ollama_base_url=_env("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        app_env=app_env,
        auth_mode=_env("AUTH_MODE", "header").lower(),
        gateway_token=_env("AUTH_GATEWAY_TOKEN"),
        jwt_secret=_env("AUTH_JWT_SECRET"),
        jwt_public_key=_env("AUTH_JWT_PUBLIC_KEY").replace("\\n", "\n"),
        jwt_algorithms=_env_list("AUTH_JWT_ALGORITHMS") or ("HS256",),
        jwt_issuer=_env("AUTH_JWT_ISSUER"),
        jwt_audience=_env("AUTH_JWT_AUDIENCE"),
        jwt_employee_claim=_env("AUTH_JWT_EMPLOYEE_CLAIM", "sub"),
        rate_limit_per_minute=_env_num("RATE_LIMIT_PER_MINUTE", "30", int),
        cors_allow_origins=_env_list("CORS_ALLOW_ORIGINS"),
        ui_enabled=_env_bool("UI_ENABLED", not production),
        metrics_enabled=_env_bool("METRICS_ENABLED", True),
        retention_days=_env_num("RETENTION_DAYS", "90", int),
        pending_ttl_minutes=_env_num("PENDING_TTL_MINUTES", "30", int, minimum=1),
    )
    validate_settings(settings)
    return settings


def validate_settings(settings: Settings) -> None:
    """Fail fast on configurations that would be unsafe or broken at runtime."""
    if settings.auth_mode not in AUTH_MODES:
        raise ValueError(f"AUTH_MODE must be one of {', '.join(AUTH_MODES)}, got {settings.auth_mode!r}")
    if settings.auth_mode == "gateway" and len(settings.gateway_token) < 32:
        raise ValueError("AUTH_MODE=gateway needs AUTH_GATEWAY_TOKEN of at least 32 characters")
    if settings.auth_mode == "jwt":
        symmetric = [a for a in settings.jwt_algorithms if a.upper().startswith("HS")]
        asymmetric = [a for a in settings.jwt_algorithms if not a.upper().startswith("HS")]
        if symmetric and asymmetric:
            raise ValueError("AUTH_JWT_ALGORITHMS must not mix HS* with RS*/ES* algorithms")
        if any(a.lower() == "none" for a in settings.jwt_algorithms):
            raise ValueError("AUTH_JWT_ALGORITHMS must not include 'none'")
        if symmetric and len(settings.jwt_secret) < 32:
            raise ValueError("AUTH_MODE=jwt with HS* needs AUTH_JWT_SECRET of at least 32 characters")
        if asymmetric and not settings.jwt_public_key:
            raise ValueError("AUTH_MODE=jwt with RS*/ES* needs AUTH_JWT_PUBLIC_KEY")
    if not settings.is_production:
        return
    if settings.auth_mode == "header":
        raise ValueError("APP_ENV=production refuses AUTH_MODE=header: X-Employee-Id alone can be forged. "
                         "Use AUTH_MODE=jwt or AUTH_MODE=gateway")
    if settings.app_today is not None:
        raise ValueError("APP_TODAY is for testing only and must be empty when APP_ENV=production")
    if "*" in settings.cors_allow_origins:
        raise ValueError("CORS_ALLOW_ORIGINS=* is not allowed when APP_ENV=production")
    vertex = _env_bool("GOOGLE_GENAI_USE_VERTEXAI", False) and _env("GOOGLE_CLOUD_PROJECT")
    key_name = {"anthropic": "anthropic", "openai": "openai", "gemini": "gemini", "google": "gemini"}.get(
        settings.llm_provider)
    anthropic_alt = _env("ANTHROPIC_AUTH_TOKEN") or _env("ANTHROPIC_FEDERATION_RULE_ID")
    if key_name and not settings.api_keys.get(key_name) and not vertex and not (
            key_name == "anthropic" and anthropic_alt):
        raise ValueError(f"LLM_PROVIDER={settings.llm_provider} needs its API key when APP_ENV=production")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
