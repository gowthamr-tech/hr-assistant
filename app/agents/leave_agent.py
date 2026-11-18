"""Leave agent: balances, leave requests, team leave, and apply / cancel proposals.

Owns the five leave tools. Reads run immediately (after the same permission pre-check the tool
enforces); writes are always returned as a proposal that the supervisor stores and only executes
after the employee confirms.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date
from typing import Optional

from app.agents import people
from app.agents.base import ChatResult, Plan, Read, clarify, refuse
from app.db import repositories as repo
from app.orchestration import guards
from app.orchestration import responses as R
from app.security.access import EMPLOYEE_REFUSAL, can_use_team_leave, can_view_leave_of
from app.tools import leave_tools as tools

log = logging.getLogger(__name__)

NAME = "leave"
WRITE_TOOLS = ("apply_leave", "cancel_leave")


class LeaveAgent:
    name = NAME
    write_tools = WRITE_TOOLS

    # ------------------------------------------------------------------ reads
    def read(self, ctx, plan: Plan) -> tuple[list[tools.ToolResult], Optional[ChatResult]]:
        """Run the plan's read tools. Returns (results, None) or ([], refusal/clarification)."""
        prepared, problem = self._prepare_reads(ctx, plan)
        if problem is not None:
            return [], problem
        return [self._run_read(ctx, read, target_id) for read, target_id in prepared], None

    def answer(self, results: list[tools.ToolResult]) -> ChatResult:
        return ChatResult(answer="\n\n".join(R.read_answer(res) for res in results), route="tool",
                          tool_calls=[res.as_call() for res in results], agent=NAME)

    def _prepare_reads(self, ctx, plan: Plan):
        prepared = []
        others: set[str] = set()
        for read in plan.reads:
            if read.tool == "get_team_leave":
                if not can_use_team_leave(ctx.requester):
                    return None, refuse(R.TEAM_ONLY_MANAGERS, NAME)
                if not (_iso(read.start_date) and _iso(read.end_date)):
                    return None, clarify("Which dates should I check for your team's leave?", NAME)
                prepared.append((read, None))
                continue
            status, value = people.resolve_target(ctx, read.target)
            if status == "refuse":
                return None, refuse(EMPLOYEE_REFUSAL, NAME)
            if status == "clarify":
                return None, clarify(value, NAME)
            if value != ctx.requester.employee_id:
                others.add(value)
            prepared.append((read, value))
        if len(others) > 1:
            return None, refuse(R.BULK, NAME)
        for _read, target_id in prepared:
            # Pre-check with the same rule the tool enforces, so a refusal carries no tool call.
            if target_id and not can_view_leave_of(ctx.requester, repo.get_employee(ctx.conn, target_id)):
                return None, refuse(EMPLOYEE_REFUSAL, NAME)
        return prepared, None

    def _run_read(self, ctx, read: Read, target_id: Optional[str]) -> tools.ToolResult:
        if read.tool == "get_leave_balance":
            return tools.get_leave_balance(ctx, target_id, read.leave_type or None)
        if read.tool == "list_leave_requests":
            return tools.list_leave_requests(ctx, target_id, read.status or None)
        return tools.get_team_leave(ctx, read.start_date, read.end_date)

    # ------------------------------------------------------------------ proposals
    def propose(self, ctx, plan: Plan, message: str, history: list[dict]) -> Optional[ChatResult]:
        kind = (plan.action or {}).get("type")
        if kind == "apply_leave":
            return self._propose_apply(ctx, plan, message, history)
        if kind == "cancel_leave":
            return self._propose_cancel(ctx, plan, message)
        return None

    def _propose_apply(self, ctx, plan: Plan, message, history) -> ChatResult:
        a = plan.action
        if people.names_someone_else(ctx, message, a.get("target", "")):
            return refuse(R.SELF_ONLY, NAME)
        user_texts = [message] + current_leave_thread(history)
        leave_type = a.get("leave_type") or ""
        if leave_type and not guards.leave_type_stated(user_texts, leave_type):
            leave_type = ""  # the interpreter guessed; the rules say ask
        start_s, end_s = str(a.get("start_date") or ""), str(a.get("end_date") or a.get("start_date") or "")
        missing = []
        if not leave_type:
            missing.append("whether this is annual or sick leave")
        if plan.date_problem in ("missing", "ambiguous") or not (_iso(start_s) and _iso(end_s)):
            missing.append("the exact start and end dates" if plan.date_problem != "ambiguous"
                           else "the exact start and end dates (the dates you gave could mean more than one day)")
        if missing:
            return clarify("To set up the request I need " + " and ".join(missing) + ".", NAME)
        start = date.fromisoformat(start_s)
        if start < ctx.today and not a.get("dates_explicit_year"):
            return clarify(f"{R.fmt_date(start_s)} has already passed (today is {R.fmt_date(ctx.today.isoformat())}). "
                           "Which dates did you mean?", NAME)
        check = tools.apply_leave(ctx, leave_type, start_s, end_s, dry_run=True)
        if check.status == "error":
            return ChatResult(answer=R.rejection(check), route="tool", tool_calls=[check.as_call()], agent=NAME)
        return ChatResult(answer=R.apply_proposal(check), route="tool",
                          tool_calls=[check.as_call("pending_confirmation")], pending_confirmation=True,
                          proposal=("apply_leave", check.args), agent=NAME)

    def _propose_cancel(self, ctx, plan: Plan, message: str) -> ChatResult:
        a = plan.action
        if people.names_someone_else(ctx, message, a.get("target", "")):
            return refuse(R.SELF_ONLY, NAME)
        request_id = str(a.get("request_id") or "").strip()
        if not request_id:
            mine = [r for r in repo.list_requests(ctx.conn, ctx.requester.employee_id)
                    if r.status in ("pending", "approved")]
            ms, me = _iso(a.get("match_start_date")), _iso(a.get("match_end_date") or a.get("match_start_date"))
            candidates = mine
            if ms and me:
                candidates = [r for r in candidates if r.start_date <= me.isoformat() and r.end_date >= ms.isoformat()]
            if a.get("leave_type"):
                candidates = [r for r in candidates if r.leave_type == a["leave_type"]]
            future = [r for r in candidates if date.fromisoformat(r.start_date) > ctx.today]
            pick = future if future else candidates
            if len(pick) == 1:
                request_id = pick[0].request_id
            elif not pick:
                if not mine:
                    return clarify("You don't have any pending or approved leave requests to cancel.", NAME)
                return clarify("I couldn't find a matching request. Your pending and approved requests are:\n"
                               + R.request_choices(mine) + "\nWhich one would you like to cancel?", NAME)
            else:
                return clarify("You have more than one matching request:\n" + R.request_choices(pick)
                               + "\nWhich one would you like to cancel?", NAME)
        check = tools.cancel_leave(ctx, request_id, dry_run=True)
        if check.status == "error":
            return ChatResult(answer=R.rejection(check), route="tool", tool_calls=[check.as_call()], agent=NAME)
        return ChatResult(answer=R.cancel_proposal(check), route="tool",
                          tool_calls=[check.as_call("pending_confirmation")], pending_confirmation=True,
                          proposal=("cancel_leave", check.args), agent=NAME)

    # ------------------------------------------------------------------ execute a confirmed proposal
    def execute(self, ctx, name: str, args: dict) -> ChatResult:
        try:
            if name == "apply_leave":
                res = tools.apply_leave(ctx, args.get("leave_type"), args.get("start_date"), args.get("end_date"))
                text = R.apply_done(res)
            else:
                res = tools.cancel_leave(ctx, args.get("request_id"))
                text = R.cancel_done(res)
        except sqlite3.Error:
            log.exception("write action failed", extra={"tool": name})
            res = tools.ToolResult(name=name, args=args, status="error", error="database error")
            text = R.SAVE_FAILED
        return ChatResult(answer=text, route="tool", tool_calls=[res.as_call()], agent=NAME)


# ====================================================================== helpers
_LEAVE_FLOW_PREFIXES = ("To set up the request", "Please confirm:")


def current_leave_thread(history: list[dict]) -> list[str]:
    """User messages of the leave request in progress: walk back while each assistant reply was
    this flow's own question or proposal. An unrelated earlier question ("what is the sick leave
    policy?") is not the user choosing a leave type."""
    texts: list[str] = []
    i = len(history) - 1
    while i >= 1 and history[i]["role"] == "assistant" and history[i - 1]["role"] == "user":
        reply = history[i]["content"]
        if not (reply.startswith(_LEAVE_FLOW_PREFIXES) or "has already passed" in reply):
            break
        texts.append(history[i - 1]["content"])
        i -= 2
    return texts


def _iso(value) -> Optional[date]:
    return tools._parse_date(value)
