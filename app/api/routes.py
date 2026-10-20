"""HTTP API: POST /chat and GET /health (Section 9)."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.db import repositories as repo
from app.db.database import connection
from app.orchestration import responses as R
from app.state import STATE

log = logging.getLogger(__name__)
router = APIRouter()

MAX_BODY_BYTES = 64 * 1024
MAX_MESSAGE_CHARS = 4000
MAX_SESSION_CHARS = 200
_HANDLER_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=32, thread_name_prefix="chat")
HANDLER_TIMEOUT_S = 29.0  # overridable for slow local models via REQUEST_DEADLINE_S


def _handler_timeout(settings) -> float:
    # Normally 29 s (the 30 s contract). A raised REQUEST_DEADLINE_S (slow local models) extends it.
    return settings.request_deadline_s + 2 if settings.request_deadline_s > 27 else HANDLER_TIMEOUT_S


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


_UI_PATH = Path(__file__).resolve().parent.parent / "static" / "index.html"


@router.get("/", include_in_schema=False)
def ui() -> HTMLResponse:
    """Minimal test UI (optional per the spec). It only calls the public /chat and /health endpoints."""
    return HTMLResponse(_UI_PATH.read_text(encoding="utf-8"))


@router.get("/health")
def health() -> JSONResponse:
    if STATE.ready:
        return JSONResponse(status_code=200, content={"status": "ok"})
    return JSONResponse(status_code=503, content={"status": "error" if STATE.failed else "starting"})


def _parse_body(raw: bytes) -> tuple[str, str] | None:
    if not raw or len(raw) > MAX_BODY_BYTES:
        return None
    try:
        body: Any = json.loads(raw)
    except Exception:  # JSONDecodeError, UnicodeDecodeError, RecursionError on deep nesting...
        return None
    if not isinstance(body, dict):
        return None
    session_id, message = body.get("session_id"), body.get("message")
    if not isinstance(session_id, str) or not isinstance(message, str):
        return None
    session_id, message = session_id.strip(), message.strip()
    try:  # lone surrogates cannot be stored or logged
        session_id.encode("utf-8"), message.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if not session_id or len(session_id) > MAX_SESSION_CHARS or not message or len(message) > MAX_MESSAGE_CHARS:
        return None
    return session_id, message


@router.post("/chat")
async def chat(request: Request) -> JSONResponse:
    request_id = str(uuid.uuid4())
    if not STATE.ready:
        return _error(503, "service is starting")
    settings = STATE.settings

    employee_id = (request.headers.get("x-employee-id") or "").strip()
    raw = await request.body()
    parsed = _parse_body(raw)

    cancelled = threading.Event()

    def _handle():
        with connection(settings.db_path) as conn:
            requester = repo.get_employee(conn, employee_id) if employee_id else None
            if requester is None:
                return _error(401, "missing or unknown X-Employee-Id")
            if parsed is None:
                return _error(400, "body must be JSON with non-empty string fields session_id and message")
            session_id, message = parsed
            owner = repo.claim_session(conn, session_id, requester.employee_id)
            if owner != requester.employee_id:
                log.warning("session ownership violation",
                            extra={"request_id": request_id, "employee_id": requester.employee_id})
                return _error(403, "session belongs to another employee")
            result = STATE.orchestrator.handle(conn, requester, session_id, message, request_id, cancelled)
            return JSONResponse(status_code=200, content={
                "request_id": request_id,
                "answer": result.answer,
                "route": result.route,
                "citations": result.citations,
                "tool_calls": result.tool_calls,
                "pending_confirmation": result.pending_confirmation,
            })

    try:
        # shield: on timeout, answer now instead of waiting for the worker thread to finish
        future = asyncio.get_running_loop().run_in_executor(_HANDLER_POOL, _handle)
        return await asyncio.wait_for(asyncio.shield(future), timeout=_handler_timeout(settings))
    except asyncio.TimeoutError:
        # The worker may still finish: tell it to keep no proposal, and drop one it already stored.
        cancelled.set()
        if parsed is not None:
            try:
                with connection(settings.db_path) as conn:
                    conn.execute("DELETE FROM assistant_pending_actions WHERE session_id = ?", (parsed[0],))
            except Exception:
                log.error("could not clear pending proposal after timeout", extra={"request_id": request_id})
        log.error("request timed out", extra={"request_id": request_id})
        return JSONResponse(status_code=200, content={
            "request_id": request_id, "answer": R.LLM_UNAVAILABLE, "route": "clarify",
            "citations": [], "tool_calls": [], "pending_confirmation": False})
    except Exception:
        log.exception("unhandled error", extra={"request_id": request_id})
        return _error(500, "internal error")
