"""The five leave tools (Section 6). Permission checks live here, inside each tool.

`ToolContext.requester` is the employee loaded from the database for the
X-Employee-Id header. No tool trusts an employee ID chosen by the LLM as proof
of authority: reads check `can_view_leave_of`, and the write tools always act
on `ctx.requester` and take no employee_id argument at all.
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from app.db import repositories as repo
from app.db.database import write_transaction
from app.db.repositories import Employee
from app.ingestion.models import DocMeta
from app.policy.calendar import choose_calendars, holiday_set, working_days
from app.security.access import EMPLOYEE_REFUSAL, can_use_team_leave, can_view_leave_of
from app.security.pii import mask_email

LEAVE_TYPES = ("annual", "sick")
STATUSES = ("pending", "approved", "cancelled")
LEAVE_YEAR = 2026
REQUEST_NOT_FOUND = "I couldn't find a leave request with that ID among your requests."


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    requester: Employee
    today: date
    docs: dict[str, DocMeta]
    holidays_by_doc: dict[str, list[date]]
    holiday_names: dict[tuple[str, date], str] = field(default_factory=dict)


@dataclass
class ToolResult:
    name: str
    args: dict
    status: str  # "success" | "error"
    data: dict = field(default_factory=dict)
    error: str = ""
    error_code: str = ""

    def as_call(self, status: Optional[str] = None) -> dict:
        return {"name": self.name, "args": dict(self.args), "status": status or self.status}


def _ok(name: str, args: dict, **data) -> ToolResult:
    return ToolResult(name=name, args=args, status="success", data=data)


def _err(name: str, args: dict, message: str, code: str) -> ToolResult:
    return ToolResult(name=name, args=args, status="error", error=message, error_code=code)


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_date(value) -> Optional[date]:
    """Only YYYY-MM-DD. Python also accepts "20261020" and "2026-W43-2", and stored dates are
    compared as strings in SQL, so any other form would slip past the overlap checks."""
    text = str(value or "").strip()
    if not _ISO_DATE.match(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _canonical(value) -> str:
    d = _parse_date(value)
    return d.isoformat() if d else str(value or "")


def _visible_name(ctx: ToolContext, target: Employee) -> dict:
    """Names and masked emails only for employees the requester may see; never phones."""
    return {"employee_id": target.employee_id, "full_name": target.full_name, "email": mask_email(target.email)}


# ------------------------------------------------------------------ read tools
def get_leave_balance(ctx: ToolContext, employee_id: str, leave_type: Optional[str] = None) -> ToolResult:
    args = {"employee_id": employee_id}
    if leave_type:
        args["leave_type"] = leave_type
    target = repo.get_employee(ctx.conn, str(employee_id or "").strip())
    if not can_view_leave_of(ctx.requester, target):
        return _err("get_leave_balance", args, EMPLOYEE_REFUSAL, "forbidden")
    if leave_type and leave_type not in LEAVE_TYPES:
        return _err("get_leave_balance", args, "Leave type must be annual or sick.", "invalid_leave_type")
    balances = repo.get_balances(ctx.conn, target.employee_id, leave_type or None)
    return _ok("get_leave_balance", args, employee=_visible_name(ctx, target), balances=balances,
               is_self=target.employee_id == ctx.requester.employee_id)


def list_leave_requests(ctx: ToolContext, employee_id: str, status: Optional[str] = None) -> ToolResult:
    args = {"employee_id": employee_id}
    if status:
        args["status"] = status
    target = repo.get_employee(ctx.conn, str(employee_id or "").strip())
    if not can_view_leave_of(ctx.requester, target):
        return _err("list_leave_requests", args, EMPLOYEE_REFUSAL, "forbidden")
    if status and status not in STATUSES:
        return _err("list_leave_requests", args, "Status must be pending, approved or cancelled.", "invalid_status")
    reqs = repo.list_requests(ctx.conn, target.employee_id, status or None)
    return _ok("list_leave_requests", args, employee=_visible_name(ctx, target),
               is_self=target.employee_id == ctx.requester.employee_id,
               requests=[_request_dict(r) for r in reqs])


def get_team_leave(ctx: ToolContext, start_date: str, end_date: str) -> ToolResult:
    start_date, end_date = _canonical(start_date), _canonical(end_date)
    args = {"start_date": start_date, "end_date": end_date}
    if not can_use_team_leave(ctx.requester):
        return _err("get_team_leave", args, "Only managers can view team leave, for their own direct reports.",
                    "forbidden")
    start, end = _parse_date(start_date), _parse_date(end_date)
    if not start or not end:
        return _err("get_team_leave", args, "Dates must be in YYYY-MM-DD format.", "invalid_date")
    if end < start:
        return _err("get_team_leave", args, "The end date must be on or after the start date.", "invalid_range")
    reports = repo.get_direct_reports(ctx.conn, ctx.requester.employee_id)
    by_id = {e.employee_id: e for e in reports}
    rows = repo.active_requests_overlapping(ctx.conn, by_id.keys(), start, end)
    return _ok("get_team_leave", args, team_size=len(reports), leave=[
        {**_visible_name(ctx, by_id[r.employee_id]), **_request_dict(r)} for r in rows
    ])


def _request_dict(r: repo.LeaveRequest) -> dict:
    return {"request_id": r.request_id, "leave_type": r.leave_type, "start_date": r.start_date,
            "end_date": r.end_date, "working_days": r.working_days, "status": r.status}


# ------------------------------------------------------------------ working days
def holidays_for(ctx: ToolContext, start: date) -> tuple[set[date], list[str]]:
    reference = max(ctx.today, start)
    calendars = choose_calendars(ctx.docs.values(), ctx.holidays_by_doc, ctx.requester, start.year, reference)
    return holiday_set(calendars, ctx.holidays_by_doc, start.year), [c.doc_id for c in calendars]


def count_working_days(ctx: ToolContext, start: date, end: date) -> tuple[int, list[tuple[date, str]]]:
    hols, cal_ids = holidays_for(ctx, start)
    skipped = sorted(
        (d, next((ctx.holiday_names[(cid, d)] for cid in cal_ids if (cid, d) in ctx.holiday_names), "public holiday"))
        for d in hols if start <= d <= end and d.weekday() < 5
    )
    return working_days(start, end, hols), skipped


# ------------------------------------------------------------------ apply_leave
def _validate_apply(ctx: ToolContext, leave_type, start_date, end_date) -> tuple[Optional[str], str, dict]:
    if leave_type not in LEAVE_TYPES:
        return "Leave type must be annual or sick.", "invalid_leave_type", {}
    start, end = _parse_date(start_date), _parse_date(end_date)
    if not start or not end:
        return "Dates must be in YYYY-MM-DD format.", "invalid_date", {}
    if start.year != LEAVE_YEAR or end.year != LEAVE_YEAR:
        return f"Leave can only be requested for dates in {LEAVE_YEAR}.", "outside_year", {}
    if start < ctx.today:
        return (f"The start date {start.isoformat()} is in the past. Leave can start today "
                f"({ctx.today.isoformat()}) or later.", "start_in_past", {})
    if end < start:
        return "The end date must be on or after the start date.", "invalid_range", {}
    days, skipped = count_working_days(ctx, start, end)
    info = {"working_days": days, "excluded_holidays": [(d.isoformat(), n) for d, n in skipped]}
    if days == 0:
        return ("That date range has no working days (it only covers weekends or public holidays), "
                "so there is nothing to request.", "zero_working_days", info)
    overlaps = repo.active_requests_overlapping(ctx.conn, [ctx.requester.employee_id], start, end)
    if overlaps:
        o = overlaps[0]
        return (f"These dates overlap your existing {o.status} {o.leave_type} leave from {o.start_date} "
                f"to {o.end_date} (request {o.request_id}).", "overlap", info)
    balance = repo.get_balances(ctx.conn, ctx.requester.employee_id, leave_type).get(leave_type)
    if balance is None:
        return f"You have no {leave_type} leave balance on record.", "no_balance", info
    info["balance_before"] = balance
    if days > balance + 1e-9:
        return (f"This request needs {days} working days but your {leave_type} leave balance is only "
                f"{_fmt(balance)} days.", "insufficient_balance", info)
    info["balance_after"] = balance - days
    return None, "", info


def apply_leave(ctx: ToolContext, leave_type: str, start_date: str, end_date: str, dry_run: bool = False) -> ToolResult:
    start_date, end_date = _canonical(start_date), _canonical(end_date)
    args = {"leave_type": leave_type, "start_date": start_date, "end_date": end_date}
    if dry_run:
        problem, code, info = _validate_apply(ctx, leave_type, start_date, end_date)
        return _err("apply_leave", args, problem, code) if problem else _ok("apply_leave", args, **info)
    # Re-validate inside the write lock: data may have changed since the proposal.
    with write_transaction(ctx.conn):
        problem, code, info = _validate_apply(ctx, leave_type, start_date, end_date)
        if problem:
            result = _err("apply_leave", args, problem, code)
        else:
            request_id = str(uuid.uuid4())
            repo.insert_request(ctx.conn, repo.LeaveRequest(
                request_id=request_id, employee_id=ctx.requester.employee_id, leave_type=leave_type,
                start_date=start_date, end_date=end_date, working_days=info["working_days"], status="pending",
                created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            ))
            repo.adjust_balance(ctx.conn, ctx.requester.employee_id, leave_type, -info["working_days"])
            result = _ok("apply_leave", args, request_id=request_id, status="pending", **info)
    return result


# ------------------------------------------------------------------ cancel_leave
def _validate_cancel(ctx: ToolContext, request_id) -> tuple[Optional[str], str, Optional[repo.LeaveRequest]]:
    req = repo.get_request(ctx.conn, str(request_id or "").strip())
    # Someone else's request and a non-existent one get the same answer.
    if req is None or req.employee_id != ctx.requester.employee_id:
        return REQUEST_NOT_FOUND, "not_found", None
    if req.status not in ("pending", "approved"):
        return f"That request is already {req.status}, so it can't be cancelled.", "not_cancellable", req
    if date.fromisoformat(req.start_date) <= ctx.today:
        return (f"That leave started on {req.start_date}. Only requests that start after today "
                f"({ctx.today.isoformat()}) can be cancelled.", "already_started", req)
    return None, "", req


def cancel_leave(ctx: ToolContext, request_id: str, dry_run: bool = False) -> ToolResult:
    args = {"request_id": request_id}
    if dry_run:
        problem, code, req = _validate_cancel(ctx, request_id)
        if problem:
            return _err("cancel_leave", args, problem, code)
        return _ok("cancel_leave", args, request=_request_dict(req))
    with write_transaction(ctx.conn):
        problem, code, req = _validate_cancel(ctx, request_id)
        if problem:
            return _err("cancel_leave", args, problem, code)
        if not repo.set_request_status(ctx.conn, req.request_id, ("pending", "approved"), "cancelled"):
            return _err("cancel_leave", args, "That request changed and can no longer be cancelled.", "conflict")
        repo.adjust_balance(ctx.conn, req.employee_id, req.leave_type, req.working_days)
        balance = repo.get_balances(ctx.conn, req.employee_id, req.leave_type).get(req.leave_type)
    return _ok("cancel_leave", args, request=_request_dict(req), restored_days=req.working_days,
               balance_after=balance)


def _fmt(x: float) -> str:
    return f"{x:g}"
