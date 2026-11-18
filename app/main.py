"""Application entry point: `uvicorn app.main:app --host 0.0.0.0 --port 8000`.

Ingestion runs in a background thread at startup; /health returns 503 until it
has finished and the orchestrator is ready. A second background thread purges
expired chat history and proposals (RETENTION_DAYS, PENDING_TTL_MINUTES).
"""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from typing import Callable, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import metrics
from app.api.middleware import RequestContextMiddleware
from app.api.routes import router, shutdown_handler_pool
from app.api.tickets import router as tickets_router
from app.config import Settings, get_settings
from app.db import repositories as repo
from app.db.database import connection, verify_and_migrate
from app.ingestion.pipeline import run_ingestion
from app.llm.client import LLMClient, build_llm_client
from app.logging_setup import setup_logging
from app.orchestration.orchestrator import Orchestrator
from app.retrieval.index import FastEmbedder
from app.security.pii import NAMES, load_known_emails
from app.security.ratelimit import RateLimiter
from app.state import STATE

log = logging.getLogger(__name__)

RETENTION_INTERVAL_S = 6 * 3600


def initialise(settings: Settings, llm: Optional[LLMClient] = None,
               embedder_factory: Optional[Callable] = None, ocr=None, kb=None) -> None:
    """Build everything the service needs. Safe to call again to re-ingest."""
    STATE.ready = False
    STATE.settings = settings
    try:
        verify_and_migrate(settings.db_path)
        with connection(settings.db_path) as conn:
            employees = repo.list_all_employees(conn)
        NAMES.load((e.employee_id, e.full_name) for e in employees)
        load_known_emails(e.email for e in employees)

        if kb is None:
            if embedder_factory is None and settings.embeddings_enabled:
                embedder_factory = lambda: FastEmbedder(settings.embedding_model)  # noqa: E731
            kwargs = {"ocr": ocr} if ocr is not None else {}
            kb = run_ingestion(settings.corpus_dir, settings.manifest_path, settings.db_path, embedder_factory,
                               **kwargs)

        if llm is None:
            try:
                llm = build_llm_client(settings)
            except Exception:
                log.error("LLM client could not be created; chat will degrade", exc_info=True)
        STATE.kb = kb
        STATE.orchestrator = Orchestrator(kb, llm, settings)
        STATE.limiter = RateLimiter(settings.rate_limit_per_minute)
        STATE.ready = True
        STATE.failed = False
        metrics.READY.set(1)
        log.info("service ready", extra={"provider": settings.llm_provider, "model": settings.llm_model,
                                         "env": settings.app_env, "auth_mode": settings.auth_mode})
    except Exception:
        STATE.failed = True
        metrics.READY.set(0)
        log.exception("startup failed")


def run_retention(settings: Settings) -> dict[str, int]:
    """One retention pass. Errors are logged, never raised: the next pass retries."""
    try:
        with connection(settings.db_path) as conn:
            counts = repo.purge_expired(conn, settings.retention_days, settings.pending_ttl_minutes)
        for table, n in counts.items():
            if n:
                metrics.RETENTION_PURGED.labels(table).inc(n)
        log.info("retention pass", extra=counts)
        return counts
    except Exception:
        log.error("retention pass failed", exc_info=True)
        return {}


def _retention_loop(settings: Settings, stop: threading.Event) -> None:
    while not stop.is_set():
        if STATE.ready:
            run_retention(settings)
            stop.wait(RETENTION_INTERVAL_S)
        else:
            stop.wait(5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_file, settings.log_format)
    if settings.auth_mode == "header":  # refused in production by validate_settings
        log.warning("AUTH_MODE=header trusts X-Employee-Id as sent; never expose this to users")
    stop = threading.Event()
    threading.Thread(target=initialise, args=(settings,), daemon=True, name="ingestion").start()
    threading.Thread(target=_retention_loop, args=(settings, stop), daemon=True, name="retention").start()
    yield
    log.info("shutting down")
    stop.set()
    shutdown_handler_pool()


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    # Reading settings here makes a bad configuration fail at start-up, not on the first request.
    settings = settings or get_settings()
    application = FastAPI(title="HR Assistant", lifespan=lifespan,
                          docs_url=None, redoc_url=None, openapi_url=None)
    application.include_router(router)
    application.include_router(tickets_router)
    if settings.cors_allow_origins:
        allowed_headers = ["Content-Type", "X-Employee-Id", "X-Request-Id", "Authorization"]
        application.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_allow_origins),
                                   allow_methods=["GET", "POST", "PATCH"], allow_headers=allowed_headers,
                                   expose_headers=["X-Request-Id", "Retry-After"], max_age=600)
    # Added last, so it is outermost: every response (CORS preflights included) gets the headers.
    application.add_middleware(RequestContextMiddleware, hsts=settings.is_production)
    return application


app = create_app()
