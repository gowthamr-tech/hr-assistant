"""Application entry point: `uvicorn app.main:app --host 0.0.0.0 --port 8000`.

Ingestion runs in a background thread at startup; /health returns 503 until it
has finished and the orchestrator is ready.
"""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from typing import Callable, Optional

from fastapi import FastAPI

from app.api.routes import router
from app.config import Settings, get_settings
from app.db import repositories as repo
from app.db.database import connection, verify_and_migrate
from app.ingestion.pipeline import run_ingestion
from app.llm.client import LLMClient, build_llm_client
from app.logging_setup import setup_logging
from app.orchestration.orchestrator import Orchestrator
from app.retrieval.index import FastEmbedder
from app.security.pii import NAMES, load_known_emails
from app.state import STATE

log = logging.getLogger(__name__)


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
        STATE.ready = True
        STATE.failed = False
        log.info("service ready", extra={"provider": settings.llm_provider, "model": settings.llm_model})
    except Exception:
        STATE.failed = True
        log.exception("startup failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_file, settings.log_format)
    threading.Thread(target=initialise, args=(settings,), daemon=True, name="ingestion").start()
    yield


app = FastAPI(title="Veltrix PeopleOps Assistant", lifespan=lifespan, docs_url=None, redoc_url=None)
app.include_router(router)
