"""HR support ticket tools. Permission checks live here, inside each tool.

Used by both the ticket agent (chat) and the /tickets REST API, so the rules are the same on
both paths:
- Any employee raises tickets for themselves (the owner is always ``ctx.requester``; there is no
  employee_id argument) and sees, comments on, closes or reopens their own tickets.
- The HR team (``can_work_tickets``) sees every ticket, works the queue and moves tickets through
  open -> in_progress -> resolved -> closed.
- Someone else's ticket and a non-existent one get the same answer.
Writes take the write lock and re-validate, like the leave write tools.
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

from app import metrics
from app.db import repositories as repo
from app.db.database import write_transaction
from app.security.access import can_view_ticket, can_work_tickets
from app.tools.leave_tools import ToolContext, ToolResult, _err, _ok

CATEGORIES = ("leave", "payroll", "benefits", "policy", "documents", "workplace", "it_access", "other")
PRIORITIES = ("low", "normal", "high", "urgent")
STATUSES = ("open", "in_progress", "resolved", "closed")
ACTIVE = ("open", "in_progress")
SCOPES = ("mine", "queue", "assigned")

MAX_SUBJECT = 120
MAX_TEXT = 2000
MAX_ACTIVE_PER_EMPLOYEE = 20
LIST_LIMIT = 50

TICKET_NOT_FOUND = "I couldn't find a ticket with that ID among the tickets you can see."
QUEUE_HR_ONLY = "Only the HR team can see the ticket queue. I can show you the tickets you raised."

TICKET_ID_RE = re.compile(r"\bTKT-[A-Z0-9]{8}\b", re.IGNORECASE)

# Who may move a ticket from one status to another.
_HR_TRANSITIONS = {
    "open": {"in_progress", "resolved", "closed"},
    "in_progress": {"open", "resolved", "closed"},
    "resolved": {"open", "closed"},
    "closed": set(),
}
_OWNER_TRANSITIONS = {
    "open": {"closed"},          # withdraw
    "in_progress": {"closed"},   # withdraw
    "resolved": {"open", "closed"},  # not fixed after all -> reopen; or confirm and close
    "closed": set(),
}
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def new_ticket_id() -> str:
    return f"TKT-{uuid.uuid4().hex[:8].upper()}"


def normalise_ticket_id(value) -> str:
    m = TICKET_ID_RE.search(str(value or ""))
    return m.group(0).upper() if m else ""


def clean_text(value, limit: int) -> str:
    """Strip control characters and surrounding blanks; cut to `limit` characters."""
    text = _CONTROL.sub(" ", str(value or "")).strip()
    return text[:limit].rstrip()


def allowed_transitions(requester, ticket: repo.Ticket) -> set[str]:
    allowed: set[str] = set()
    if can_work_tickets(requester):
        allowed |= _HR_TRANSITIONS.get(ticket.status, set())
    if ticket.employee_id == requester.employee_id:
        allowed |= _OWNER_TRANSITIONS.get(ticket.status, set())
    return allowed


def ticket_dict(t: repo.Ticket, with_description: bool = False) -> dict:
    d = {"ticket_id": t.ticket_id, "raised_by": t.employee_id, "category": t.category, "priority": t.priority,
         "subject": t.subject, "status": t.status, "assignee_id": t.assignee_id, "created_at": t.created_at,
         "updated_at": t.updated_at}
    if with_description:
        d["description"] = t.description
    return d


def _visible_ticket(ctx: ToolContext, ticket_id) -> Optional[repo.Ticket]:
    t = repo.get_ticket(ctx.conn, normalise_ticket_id(ticket_id))
    if t is None or not can_view_ticket(ctx.requester, t.employee_id):
        return None
    return t


# ------------------------------------------------------------------ create_ticket
def _validate_create(ctx: ToolContext, category, priority, subject, description) -> tuple[Optional[str], str]:
    if category not in CATEGORIES:
        return "Category must be one of: " + ", ".join(CATEGORIES) + ".", "invalid_category"
    if priority not in PRIORITIES:
        return "Priority must be one of: " + ", ".join(PRIORITIES) + ".", "invalid_priority"
    if not subject:
        return "A ticket needs a short subject.", "missing_subject"
    if not description:
        return "A ticket needs a description of the problem.", "missing_description"
    if repo.count_tickets(ctx.conn, ctx.requester.employee_id, ACTIVE) >= MAX_ACTIVE_PER_EMPLOYEE:
        return (f"You already have {MAX_ACTIVE_PER_EMPLOYEE} open tickets. Please close ones you no longer need "
                "before opening another.", "too_many_open")
    return None, ""


def create_ticket(ctx: ToolContext, category: str, priority: str, subject: str, description: str,
                  dry_run: bool = False) -> ToolResult:
    subject, description = clean_text(subject, MAX_SUBJECT), clean_text(description, MAX_TEXT)
    args = {"category": category, "priority": priority, "subject": subject, "description": description}
    if dry_run:
        problem, code = _validate_create(ctx, category, priority, subject, description)
        return _err("create_ticket", args, problem, code) if problem else _ok("create_ticket", args)
    with write_transaction(ctx.conn):
        problem, code = _validate_create(ctx, category, priority, subject, description)
        if problem:
            return _err("create_ticket", args, problem, code)
        now = repo._now_iso()
        ticket = repo.Ticket(ticket_id=new_ticket_id(), employee_id=ctx.requester.employee_id, category=category,
                             priority=priority, subject=subject, description=description, status="open",
                             assignee_id=None, created_at=now, updated_at=now)
        repo.insert_ticket(ctx.conn, ticket)
        repo.add_ticket_event(ctx.conn, ticket.ticket_id, ctx.requester.employee_id, "created", subject)
    metrics.TICKET_WRITES.labels("create").inc()
    return _ok("create_ticket", args, ticket=ticket_dict(ticket, with_description=True))


# ------------------------------------------------------------------ list_tickets / get_ticket
def list_tickets(ctx: ToolContext, scope: str = "mine", status: Optional[str] = None) -> ToolResult:
    scope = scope or "mine"
    args = {"scope": scope}
    if status:
        args["status"] = status
    if scope not in SCOPES:
        return _err("list_tickets", args, "Scope must be mine, queue or assigned.", "invalid_scope")
    if status and status not in STATUSES:
        return _err("list_tickets", args, "Status must be open, in_progress, resolved or closed.", "invalid_status")
    if scope != "mine" and not can_work_tickets(ctx.requester):
        return _err("list_tickets", args, QUEUE_HR_ONLY, "forbidden")
    statuses = [status] if status else None
    if scope == "mine":
        rows = repo.list_tickets(ctx.conn, employee_id=ctx.requester.employee_id, statuses=statuses, limit=LIST_LIMIT)
    else:
        assignee = ctx.requester.employee_id if scope == "assigned" else None
        rows = repo.list_tickets(ctx.conn, assignee_id=assignee, statuses=statuses or ACTIVE, limit=LIST_LIMIT)
    return _ok("list_tickets", args, tickets=[ticket_dict(t) for t in rows], truncated=len(rows) >= LIST_LIMIT)


def get_ticket(ctx: ToolContext, ticket_id: str) -> ToolResult:
    args = {"ticket_id": normalise_ticket_id(ticket_id) or str(ticket_id or "")}
    t = _visible_ticket(ctx, ticket_id)
    if t is None:
        return _err("get_ticket", args, TICKET_NOT_FOUND, "not_found")
    events = [{"author_id": e.author_id, "kind": e.kind, "body": e.body, "created_at": e.created_at}
              for e in repo.ticket_events(ctx.conn, t.ticket_id)]
    return _ok("get_ticket", args, ticket=ticket_dict(t, with_description=True), events=events,
               allowed_statuses=sorted(allowed_transitions(ctx.requester, t)))


# ------------------------------------------------------------------ comment_ticket
def _validate_comment(ctx: ToolContext, ticket_id, comment) -> tuple[Optional[str], str, Optional[repo.Ticket]]:
    t = _visible_ticket(ctx, ticket_id)
    if t is None:
        return TICKET_NOT_FOUND, "not_found", None
    if not comment:
        return "The comment is empty.", "missing_comment", t
    if t.status == "closed":
        return "That ticket is closed, so it can't take new comments. Please open a new ticket.", "closed", t
    return None, "", t


def comment_ticket(ctx: ToolContext, ticket_id: str, comment: str, dry_run: bool = False) -> ToolResult:
    comment = clean_text(comment, MAX_TEXT)
    args = {"ticket_id": normalise_ticket_id(ticket_id) or str(ticket_id or ""), "comment": comment}
    if dry_run:
        problem, code, t = _validate_comment(ctx, ticket_id, comment)
        if problem:
            return _err("comment_ticket", args, problem, code)
        return _ok("comment_ticket", args, ticket=ticket_dict(t))
    with write_transaction(ctx.conn):
        problem, code, t = _validate_comment(ctx, ticket_id, comment)
        if problem:
            return _err("comment_ticket", args, problem, code)
        repo.add_ticket_event(ctx.conn, t.ticket_id, ctx.requester.employee_id, "comment", comment)
        repo.touch_ticket(ctx.conn, t.ticket_id)
    metrics.TICKET_WRITES.labels("comment").inc()
    return _ok("comment_ticket", args, ticket=ticket_dict(t))


# ------------------------------------------------------------------ update_ticket_status
def _validate_status(ctx: ToolContext, ticket_id, status) -> tuple[Optional[str], str, Optional[repo.Ticket]]:
    t = _visible_ticket(ctx, ticket_id)
    if t is None:
        return TICKET_NOT_FOUND, "not_found", None
    if status not in STATUSES:
        return "Status must be open, in_progress, resolved or closed.", "invalid_status", t
    if status == t.status:
        return f"That ticket is already {status.replace('_', ' ')}.", "unchanged", t
    allowed = allowed_transitions(ctx.requester, t)
    if status not in allowed:
        if t.status == "closed":
            return "That ticket is closed. Please open a new ticket instead.", "not_allowed", t
        if not can_work_tickets(ctx.requester):
            return ("Only the HR team can mark a ticket in progress or resolved. You can close your own ticket, "
                    "or reopen it once it's resolved."), "not_allowed", t
        return (f"A ticket that is {t.status.replace('_', ' ')} can't be moved to {status.replace('_', ' ')}.",
                "not_allowed", t)
    return None, "", t


def update_ticket_status(ctx: ToolContext, ticket_id: str, status: str, note: str = "",
                         dry_run: bool = False) -> ToolResult:
    note = clean_text(note, MAX_TEXT)
    args = {"ticket_id": normalise_ticket_id(ticket_id) or str(ticket_id or ""), "status": status}
    if note:
        args["note"] = note
    if dry_run:
        problem, code, t = _validate_status(ctx, ticket_id, status)
        if problem:
            return _err("update_ticket_status", args, problem, code)
        return _ok("update_ticket_status", args, ticket=ticket_dict(t), previous_status=t.status)
    me = ctx.requester.employee_id
    with write_transaction(ctx.conn):
        problem, code, t = _validate_status(ctx, ticket_id, status)
        if problem:
            return _err("update_ticket_status", args, problem, code)
        assignee = t.assignee_id
        # The HR member who starts working on an unassigned ticket takes it.
        if status == "in_progress" and assignee is None and can_work_tickets(ctx.requester):
            assignee = me
        if not repo.update_ticket(ctx.conn, t.ticket_id, (t.status,), status=status, assignee_id=assignee):
            return _err("update_ticket_status", args, "That ticket changed in the meantime. Please try again.",
                        "conflict")
        repo.add_ticket_event(ctx.conn, t.ticket_id, me, "status", f"{t.status} -> {status}")
        if assignee != t.assignee_id:
            repo.add_ticket_event(ctx.conn, t.ticket_id, me, "assigned", assignee or "")
        if note:
            repo.add_ticket_event(ctx.conn, t.ticket_id, me, "comment", note)
        updated = repo.get_ticket(ctx.conn, t.ticket_id)
    metrics.TICKET_WRITES.labels("status").inc()
    return _ok("update_ticket_status", args, ticket=ticket_dict(updated), previous_status=t.status)
