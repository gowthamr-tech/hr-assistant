"""Ticket agent: HR support tickets over chat.

Reads (list, view) run immediately. Writes (create, comment, status change) are returned as a
proposal like leave writes, so nothing is filed or changed until the employee replies "yes".
Answers come from fixed templates; ticket text never goes back through an LLM.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from app.agents.base import ChatResult, Plan, clarify, refuse
from app.db import repositories as repo
from app.orchestration import responses as R
from app.security.access import can_work_tickets
from app.tools import ticket_tools as tt
from app.tools.leave_tools import ToolResult

log = logging.getLogger(__name__)

NAME = "ticket"
WRITE_TOOLS = ("create_ticket", "comment_ticket", "update_ticket_status")


class TicketAgent:
    name = NAME
    write_tools = WRITE_TOOLS

    def handle(self, ctx, plan: Plan, message: str) -> ChatResult:
        t = plan.ticket or {}
        kind = t.get("type") or "none"
        if kind == "create":
            return self._propose_create(ctx, t)
        if kind == "list":
            return self._list(ctx, t)
        if kind in ("view", "comment", "update_status"):
            ticket_id, problem = self._pick_ticket(ctx, t, message, kind)
            if problem is not None:
                return problem
            if kind == "view":
                res = tt.get_ticket(ctx, ticket_id)
                return _tool(R.ticket_detail_answer(res), res)
            if kind == "comment":
                return self._propose_comment(ctx, ticket_id, t)
            return self._propose_status(ctx, ticket_id, t)
        return clarify(R.TICKET_HELP, NAME)

    # ------------------------------------------------------------------ reads
    def _list(self, ctx, t: dict) -> ChatResult:
        scope = t.get("scope") or "mine"
        if scope != "mine" and not can_work_tickets(ctx.requester):
            return refuse(tt.QUEUE_HR_ONLY, NAME)  # pre-check, so the refusal carries no tool call
        res = tt.list_tickets(ctx, scope, t.get("status") or None)
        return _tool(R.tickets_answer(res), res)

    def _pick_ticket(self, ctx, t: dict, message: str, kind: str) -> tuple[str, Optional[ChatResult]]:
        """The ticket the message is about: the given ID, or the requester's only active ticket."""
        ticket_id = tt.normalise_ticket_id(t.get("ticket_id")) or tt.normalise_ticket_id(message)
        if ticket_id:
            return ticket_id, None
        mine = repo.list_tickets(ctx.conn, employee_id=ctx.requester.employee_id,
                                 statuses=("open", "in_progress", "resolved"), limit=10)
        if len(mine) == 1:
            return mine[0].ticket_id, None
        if not mine:
            if kind == "view":
                return "", clarify("You don't have any open tickets. " + R.TICKET_NEED_ID, NAME)
            return "", clarify(R.TICKET_NEED_ID, NAME)
        return "", clarify("Which ticket do you mean?\n" + R.ticket_choices(mine), NAME)

    # ------------------------------------------------------------------ proposals
    def _propose_create(self, ctx, t: dict) -> ChatResult:
        subject = tt.clean_text(t.get("subject"), tt.MAX_SUBJECT)
        description = tt.clean_text(t.get("description"), tt.MAX_TEXT)
        if not subject and not description:
            return clarify(R.TICKET_NEED_DETAILS, NAME)
        subject = subject or tt.clean_text(description, 80)
        description = description or subject
        category = t.get("category") if t.get("category") in tt.CATEGORIES else "other"
        priority = t.get("priority") if t.get("priority") in tt.PRIORITIES else "normal"
        check = tt.create_ticket(ctx, category, priority, subject, description, dry_run=True)
        return _proposal(check, R.ticket_create_proposal)

    def _propose_comment(self, ctx, ticket_id: str, t: dict) -> ChatResult:
        comment = tt.clean_text(t.get("comment"), tt.MAX_TEXT)
        if not comment:
            return clarify(R.TICKET_NEED_COMMENT, NAME)
        check = tt.comment_ticket(ctx, ticket_id, comment, dry_run=True)
        return _proposal(check, R.ticket_comment_proposal)

    def _propose_status(self, ctx, ticket_id: str, t: dict) -> ChatResult:
        status = t.get("status") or ""
        if status not in tt.STATUSES:
            return clarify(R.TICKET_NEED_STATUS, NAME)
        check = tt.update_ticket_status(ctx, ticket_id, status, t.get("comment") or "", dry_run=True)
        return _proposal(check, R.ticket_status_proposal)

    # ------------------------------------------------------------------ execute a confirmed proposal
    def execute(self, ctx, name: str, args: dict) -> ChatResult:
        try:
            if name == "create_ticket":
                res = tt.create_ticket(ctx, args.get("category"), args.get("priority"), args.get("subject"),
                                       args.get("description"))
                text = R.ticket_created(res)
            elif name == "comment_ticket":
                res = tt.comment_ticket(ctx, args.get("ticket_id"), args.get("comment"))
                text = R.ticket_commented(res)
            else:
                res = tt.update_ticket_status(ctx, args.get("ticket_id"), args.get("status"), args.get("note", ""))
                text = R.ticket_status_done(res)
        except sqlite3.Error:
            log.exception("ticket write failed", extra={"tool": name})
            res = ToolResult(name=name, args=args, status="error", error="database error")
            text = R.SAVE_FAILED
        return ChatResult(answer=text, route="tool", tool_calls=[res.as_call()], agent=NAME)


def _tool(text: str, res: ToolResult) -> ChatResult:
    return ChatResult(answer=text, route="tool", tool_calls=[res.as_call()], agent=NAME)


def _proposal(check: ToolResult, template) -> ChatResult:
    if check.status == "error":
        return _tool(R.ticket_rejection(check), check)
    return ChatResult(answer=template(check), route="tool", tool_calls=[check.as_call("pending_confirmation")],
                      pending_confirmation=True, proposal=(check.name, check.args), agent=NAME)
