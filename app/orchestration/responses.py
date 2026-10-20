"""Fixed messages and deterministic answer templates for tool results."""
from __future__ import annotations

from datetime import date

from app.tools.leave_tools import ToolResult

SCOPE_HELP = ("I can help with Veltrix HR policies and holidays, and with your leave: balances, "
              "leave requests, and applying for or cancelling leave.")
OFF_TOPIC = "Sorry, that's outside what I can help with. " + SCOPE_HELP
MANIPULATION = ("I can't do that. My rules are fixed, I don't share internal instructions, and I only show "
                "data you're permitted to see. " + SCOPE_HELP)
BULK = ("I can't share data about many employees at once. I can show one employee's leave at a time "
        "(if you're allowed to see it), and managers can see which of their direct reports are on leave "
        "for a date range.")
PERSONAL = "I can't share employees' contact details or other personal information. " + SCOPE_HELP
TEAM_ONLY_MANAGERS = "Team leave is only available to managers, for their own direct reports."
NO_ANSWER = ("The HR documents available to me don't cover that question, so I can't answer it reliably. "
             "Please contact the HR team for help.")
GREETING = "Hello! " + SCOPE_HELP + " What would you like to know?"
LLM_UNAVAILABLE = ("Sorry, I couldn't process that request right now. Please try again in a moment, or "
                   "contact the HR team.")
SELF_ONLY = "I can only apply for or cancel your own leave."
CLARIFY_EMPLOYEE_OR_PERIOD = ("Could you give me a bit more detail? Tell me which employee (by employee ID) "
                              "or which dates you mean.")
PROPOSAL_DISCARDED = "Okay, I haven't submitted anything and nothing was changed. Is there anything else I can help with?"


def fmt_days(x: float) -> str:
    return f"{x:g}"


def fmt_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b %Y')}"


def _who(result: ToolResult) -> str:
    if result.data.get("is_self"):
        return "You have"
    e = result.data.get("employee", {})
    return f"{e.get('full_name', '')} ({e.get('employee_id', '')}) has"


def balance_answer(result: ToolResult) -> str:
    balances = result.data.get("balances", {})
    if not balances:
        return f"{_who(result)} no leave balance on record for that leave type."
    parts = [f"{fmt_days(v)} days of {k} leave" for k, v in sorted(balances.items())]
    return f"{_who(result)} {' and '.join(parts)} remaining."


def _request_line(r: dict) -> str:
    return (f"- {r['leave_type'].capitalize()} leave, {fmt_date(r['start_date'])} to {fmt_date(r['end_date'])} "
            f"({r['working_days']} working days), {r['status']} (request ID {r['request_id']})")


def requests_answer(result: ToolResult) -> str:
    reqs = result.data.get("requests", [])
    status = result.args.get("status")
    subject = "You have" if result.data.get("is_self") else _who(result)
    if not reqs:
        return f"{subject} no {status + ' ' if status else ''}leave requests."
    return f"{subject} {len(reqs)} {status + ' ' if status else ''}leave request(s):\n" + "\n".join(
        _request_line(r) for r in reqs)


def team_answer(result: ToolResult) -> str:
    rows = result.data.get("leave", [])
    s, e = result.args["start_date"], result.args["end_date"]
    if not rows:
        return (f"None of your {result.data.get('team_size', 0)} direct reports have pending or approved leave "
                f"between {fmt_date(s)} and {fmt_date(e)}.")
    lines = [f"- {r['full_name']} ({r['employee_id']}): {r['leave_type']} leave, {fmt_date(r['start_date'])} to "
             f"{fmt_date(r['end_date'])} ({r['working_days']} working days), {r['status']}" for r in rows]
    return f"Direct reports with pending or approved leave between {fmt_date(s)} and {fmt_date(e)}:\n" + "\n".join(lines)


def read_answer(result: ToolResult) -> str:
    if result.status == "error":
        return result.error
    if result.name == "get_leave_balance":
        return balance_answer(result)
    if result.name == "list_leave_requests":
        return requests_answer(result)
    if result.name == "get_team_leave":
        return team_answer(result)
    return ""


def _holiday_note(data: dict) -> str:
    hols = data.get("excluded_holidays") or []
    if not hols:
        return ""
    names = ", ".join(f"{fmt_date(d)} ({n})" for d, n in hols)
    return f" Public holidays not counted: {names}."


def apply_proposal(result: ToolResult) -> str:
    a, d = result.args, result.data
    return (f"Please confirm: {a['leave_type']} leave from {fmt_date(a['start_date'])} to {fmt_date(a['end_date'])}, "
            f"{d['working_days']} working day(s).{_holiday_note(d)} Your {a['leave_type']} balance would go from "
            f"{fmt_days(d['balance_before'])} to {fmt_days(d['balance_after'])} days. "
            "Reply \"yes\" to submit it, or tell me what to change.")


def apply_done(result: ToolResult) -> str:
    if result.status == "error":
        return f"I couldn't submit that leave request: {result.error}"
    a, d = result.args, result.data
    return (f"Done. Your {a['leave_type']} leave request from {fmt_date(a['start_date'])} to "
            f"{fmt_date(a['end_date'])} ({d['working_days']} working day(s)) has been created with status pending. "
            f"Request ID: {d['request_id']}. Your remaining {a['leave_type']} balance is "
            f"{fmt_days(d['balance_after'])} days.")


def cancel_proposal(result: ToolResult) -> str:
    r = result.data["request"]
    return (f"Please confirm: cancel your {r['status']} {r['leave_type']} leave from {fmt_date(r['start_date'])} to "
            f"{fmt_date(r['end_date'])} ({r['working_days']} working day(s), request ID {r['request_id']}). "
            f"The {r['working_days']} day(s) will be returned to your {r['leave_type']} balance. "
            "Reply \"yes\" to cancel it.")


def cancel_done(result: ToolResult) -> str:
    if result.status == "error":
        return f"I couldn't cancel that request: {result.error}"
    r = result.data["request"]
    return (f"Done. Your {r['leave_type']} leave from {fmt_date(r['start_date'])} to {fmt_date(r['end_date'])} "
            f"(request ID {r['request_id']}) is cancelled and {result.data['restored_days']} day(s) were returned. "
            f"Your {r['leave_type']} balance is now {fmt_days(result.data['balance_after'])} days.")


def rejection(result: ToolResult) -> str:
    verb = "apply for that leave" if result.name == "apply_leave" else "cancel that request"
    return f"I can't {verb}: {result.error}"


def request_choices(reqs: list) -> str:
    return "\n".join(
        f"- {r.leave_type} leave, {fmt_date(r.start_date)} to {fmt_date(r.end_date)}, {r.status} (request ID {r.request_id})"
        for r in reqs)
