"""Resolving the people a message refers to, limited to who the requester may see.

Shared by the supervisor (history redaction), the leave agent (read targets, self-only writes)
and the policy agent (redacting the employee's words before they reach the composer).
"""
from __future__ import annotations

import re

from app.db import repositories as repo
from app.security.pii import NAMES

EMPLOYEE_ID = re.compile(r"^[A-Za-z]\d{3,6}$")
SELF_WORDS = {"", "self", "me", "my", "mine", "i", "myself", "requester", "you"}


def visible_ids(ctx) -> set[str]:
    me = ctx.requester
    if me.role == "hr":
        return {e.employee_id for e in repo.list_all_employees(ctx.conn)}
    ids = {me.employee_id}
    if me.role == "manager":
        ids |= {e.employee_id for e in repo.get_direct_reports(ctx.conn, me.employee_id)}
    return ids


def resolve_target(ctx, target: str) -> tuple[str, str]:
    """("ok", employee_id) | ("refuse", "") | ("clarify", question)."""
    t = (target or "").strip()
    me = ctx.requester
    if t.lower() in SELF_WORDS:
        return "ok", me.employee_id
    if EMPLOYEE_ID.match(t):
        return "ok", t.upper()
    visible = [me]
    if me.role == "manager":
        visible += repo.get_direct_reports(ctx.conn, me.employee_id)
    elif me.role == "hr":
        visible = repo.list_all_employees(ctx.conn)
    words = [w for w in re.split(r"\s+", t.lower()) if w]
    matches = [e for e in visible if all(w in e.full_name.lower().split() for w in words)]
    if len(matches) == 1:
        return "ok", matches[0].employee_id
    if not matches:
        return "refuse", ""
    options = ", ".join(f"{e.full_name} ({e.employee_id})" for e in matches[:6])
    return "clarify", f"Which employee do you mean: {options}?"


def names_someone_else(ctx, message: str, target: str) -> bool:
    """Write tools act for the requester only. True when the request names another person."""
    t = (target or "").strip()
    if t and t.lower() not in SELF_WORDS:
        status, value = resolve_target(ctx, t)
        if status != "ok" or value != ctx.requester.employee_id:
            return True
    for emp_id in re.findall(r"(?<![\w-])[Ee]\d{3,6}(?![\w-])", message):
        if emp_id.upper() != ctx.requester.employee_id:
            return True
    return NAMES.mentions_other_than(message, ctx.requester.employee_id)
