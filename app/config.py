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
    return raw.lower() in {"1", "true", "yes", "on"}


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
        raise ValueError(f"Unsupported LLM_PROVIDER {provider!r}; use anthropic, openai or gemini")
    model = _env("LLM_MODEL", _DEFAULT_MODELS.get(provider, ""))

    return Settings(
        corpus_dir=_env("CORPUS_DIR", "./data/corpus"),
        manifest_path=_env("MANIFEST_PATH", "./data/manifest.json"),
        db_path=_env("DB_PATH", "./data/veltrix.db"),
        app_today=app_today,
        llm_provider=provider,
        llm_model=model,
        llm_router_model=_env("LLM_ROUTER_MODEL", model),
        llm_timeout_s=float(_env("LLM_TIMEOUT_S", "20")),
        llm_max_retries=int(_env("LLM_MAX_RETRIES", "1")),
        anthropic_effort=_env("ANTHROPIC_EFFORT", "low"),
        anthropic_refusal_fallback=_env_bool("ANTHROPIC_REFUSAL_FALLBACK", True),
        embeddings_enabled=_env_bool("EMBEDDINGS_ENABLED", True),
        embedding_model=_env("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
        log_level=_env("LOG_LEVEL", "INFO"),
        log_file=_env("LOG_FILE", ""),
        log_format=_env("LOG_FORMAT", "auto"),
        request_deadline_s=float(_env("REQUEST_DEADLINE_S", "27")),
        api_keys={
            "anthropic": _env("ANTHROPIC_API_KEY"),
            "openai": _env("OPENAI_API_KEY"),
            "gemini": _env("GOOGLE_API_KEY") or _env("GEMINI_API_KEY"),
        },
        ollama_base_url=_env("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
