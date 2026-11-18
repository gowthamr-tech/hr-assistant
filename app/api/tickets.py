"""HTTP API for HR support tickets, for the HR team's queue and for portals / integrations.

    GET   /tickets?scope=mine|queue|assigned&status=...   list
    POST  /tickets                                          {subject, description, category?, priority?}
    GET   /tickets/{ticket_id}                              details and history
    POST  /tickets/{ticket_id}/comments                     {comment}
    PATCH /tickets/{ticket_id}                              {status, note?}

Same authentication and rate limit as /chat, and the same tools (``app.tools.ticket_tools``) the
ticket agent uses, so permissions are identical on both paths. An explicit API call is its own
confirmation, so writes here apply directly.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app import metrics
from app.api.middleware import request_id_from
from app.api.routes import _read_body
from app.db import repositories as repo
from app.db.database import connection
from app.security.auth import UNAUTHENTICATED, authenticate
from app.state import STATE
from app.tools import ticket_tools as tt
from app.tools.leave_tools import ToolContext, ToolResult

log = logging.getLogger(__name__)
router = APIRouter(prefix="/tickets")

_STATUS_FOR_ERROR = {
    "not_found": 404,
    "forbidden": 403,
    "not_allowed": 409,
    "closed": 409,
    "unchanged": 409,
    "conflict": 409,
    "too_many_open": 429,
}


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


def _result(res: ToolResult, ok_status: int = 200) -> JSONResponse:
    if res.status == "error":
        return _error(_STATUS_FOR_ERROR.get(res.error_code, 400), res.error)
    return JSONResponse(status_code=ok_status, content=res.data)


async def _json_body(request: Request) -> Optional[dict]:
    raw = await _read_body(request)
    if not raw:
        return None
    try:
        body: Any = json.loads(raw)
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _str_field(body: dict, key: str, default: str = "") -> Optional[str]:
    value = body.get(key, default)
    return value.strip() if isinstance(value, str) else None


async def _run(request: Request, work: Callable[[ToolContext], JSONResponse]) -> JSONResponse:
    """Authenticate, rate-limit, load the requester from the database, then run `work`."""
    request_id = request_id_from(request.scope)
    if not STATE.ready:
        return _error(503, "service is starting")
    settings = STATE.settings
    auth = authenticate(request.headers, settings)
    employee_id = auth.employee_id
    if employee_id is None:
        if settings.auth_mode != "header":
            metrics.AUTH_FAILURES.inc()
            log.warning("authentication failed", extra={"request_id": request_id, "reason": auth.reason})
            return _error(401, UNAUTHENTICATED)
        return _error(401, "missing or unknown X-Employee-Id")
    if STATE.limiter is not None:
        retry_after = STATE.limiter.check(employee_id)
        if retry_after:
            metrics.RATE_LIMITED.inc()
            response = _error(429, "too many requests")
            response.headers["Retry-After"] = str(int(retry_after) + 1)
            return response

    def _handle() -> JSONResponse:
        with connection(settings.db_path) as conn:
            requester = repo.get_employee(conn, employee_id)
            if requester is None:
                if settings.auth_mode != "header":
                    metrics.AUTH_FAILURES.inc()
                    return _error(401, UNAUTHENTICATED)
                return _error(401, "missing or unknown X-Employee-Id")
            kb = STATE.kb
            ctx = ToolContext(conn=conn, requester=requester, today=settings.today(),
                              docs=kb.docs if kb else {}, holidays_by_doc=kb.holidays_by_doc if kb else {})
            response = work(ctx)
            log.info("ticket api", extra={"request_id": request_id, "employee_id": requester.employee_id,
                                          "method": request.method, "status": response.status_code})
            return response

    try:
        return await run_in_threadpool(_handle)
    except Exception:
        log.exception("ticket api failed", extra={"request_id": request_id})
        return _error(500, "internal error")


@router.get("")
async def list_tickets(request: Request) -> JSONResponse:
    scope = request.query_params.get("scope", "mine")
    status = request.query_params.get("status") or None
    return await _run(request, lambda ctx: _result(tt.list_tickets(ctx, scope, status)))


@router.post("")
async def create_ticket(request: Request) -> JSONResponse:
    body = await _json_body(request)
    if body is None:
        return _error(400, "body must be a JSON object")
    subject, description = _str_field(body, "subject"), _str_field(body, "description")
    category, priority = _str_field(body, "category", "other"), _str_field(body, "priority", "normal")
    if None in (subject, description, category, priority):
        return _error(400, "subject, description, category and priority must be strings")
    return await _run(request, lambda ctx: _result(
        tt.create_ticket(ctx, category or "other", priority or "normal", subject, description), 201))


@router.get("/{ticket_id}")
async def get_ticket(request: Request, ticket_id: str) -> JSONResponse:
    return await _run(request, lambda ctx: _result(tt.get_ticket(ctx, _exact_id(ticket_id))))


@router.post("/{ticket_id}/comments")
async def comment_ticket(request: Request, ticket_id: str) -> JSONResponse:
    body = await _json_body(request)
    comment = _str_field(body, "comment") if body is not None else None
    if not comment:
        return _error(400, "body must be JSON with a non-empty string field comment")
    return await _run(request, lambda ctx: _result(tt.comment_ticket(ctx, _exact_id(ticket_id), comment), 201))


@router.patch("/{ticket_id}")
async def update_ticket(request: Request, ticket_id: str) -> JSONResponse:
    body = await _json_body(request)
    status = _str_field(body, "status") if body is not None else None
    note = _str_field(body, "note") if body is not None else None
    if not status or note is None:
        return _error(400, "body must be JSON with a string field status (and an optional string note)")
    return await _run(request, lambda ctx: _result(tt.update_ticket_status(ctx, _exact_id(ticket_id), status, note)))


def _exact_id(raw: str) -> str:
    """A path ID must be a whole ticket ID, not text that merely contains one."""
    value = (raw or "").strip()
    normalised = tt.normalise_ticket_id(value)
    return normalised if normalised and len(value) == len(normalised) else "-"
