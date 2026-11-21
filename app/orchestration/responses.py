"""Fixed messages and deterministic answer templates for tool results."""
from __future__ import annotations

from datetime import date

from app.tools.leave_tools import ToolResult

SCOPE_HELP = ("I can help with HR policies and holidays, with your leave: balances, "
              "leave requests, and applying for or cancelling leave, and with HR support tickets: "
              "raising one and following it up.")
OFF_TOPIC = "Sorry, that's outside what I can help with. " + SCOPE_HELP
MANIPULATION = ("I can't do that. My rules are fixed, I don't share internal instructions, and I only show "
                "data you're permitted to see. " + SCOPE_HELP)
BULK = ("I can't share data about many employees at once. I can show one employee's leave at a time "
        "(if you're allowed to see it), and managers can see which of their direct reports are on leave "
        "for a date range.")
PERSONAL = "I can't share employees' contact details or other personal information. " + SCOPE_HELP
TEAM_ONLY_MANAGERS = "Team leave is only available to managers, for their own direct reports."
NO_ANSWER = ("The HR documents available to me don't cover that question, so I can't answer it reliably. "
             "Please contact the HR team for help, or ask me to raise an HR support ticket for it.")
POLICY_PART_UNCOVERED = ("The HR documents don't cover the policy part of your question; please contact the HR team "
                         "for that, or ask me to raise an HR support ticket.")
SAVE_FAILED = "Something went wrong while saving, and nothing was changed. Please try again."
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


# ------------------------------------------------------------------ support tickets
TICKET_HELP = ("I can raise an HR support ticket for you, show your tickets and their status, add a comment "
               "to a ticket, or close or reopen one. What would you like to do?")
TICKET_NEED_DETAILS = "Sure, I can raise an HR support ticket. What is the problem or request you'd like HR to look at?"
TICKET_NEED_ID = "Which ticket do you mean? Please give its ticket ID (it looks like TKT-1A2B3C4D)."
TICKET_NEED_COMMENT = "What would you like to add to the ticket?"
TICKET_NEED_STATUS = "What should the ticket's status be: open, in progress, resolved or closed?"


def _label(value: str) -> str:
    return (value or "").replace("_", " ")


def _ticket_line(t: dict, show_owner: bool) -> str:
    extra = ""
    if show_owner:
        extra = f", raised by {t['raised_by']}" + (f", assigned to {t['assignee_id']}" if t.get("assignee_id")
                                                   else ", unassigned")
    return (f"- {t['ticket_id']}: {t['subject']} ({_label(t['status'])}, {t['priority']} priority, "
            f"{_label(t['category'])}, opened {fmt_date(t['created_at'][:10])}{extra})")


def tickets_answer(result: ToolResult) -> str:
    if result.status == "error":
        return result.error
    rows = result.data.get("tickets", [])
    scope, status = result.args.get("scope", "mine"), result.args.get("status")
    status_text = f"{_label(status)} " if status else ("open or in-progress " if scope != "mine" else "")
    where = {"mine": "You have", "queue": "The HR queue has", "assigned": "You are assigned"}[scope]
    if not rows:
        return f"{where} no {status_text}tickets."
    more = f"\n(Showing the first {len(rows)}.)" if result.data.get("truncated") else ""
    return (f"{where} {len(rows)} {status_text}ticket(s):\n"
            + "\n".join(_ticket_line(t, scope != "mine") for t in rows) + more)


_EVENT_TEXT = {
    "created": "opened the ticket",
    "comment": "commented",
    "status": "changed the status",
    "assigned": "assigned it",
}


def ticket_detail_answer(result: ToolResult) -> str:
    if result.status == "error":
        return result.error
    t, events = result.data["ticket"], result.data.get("events", [])
    lines = [f"Ticket {t['ticket_id']}: {t['subject']}",
             f"Status: {_label(t['status'])} · Priority: {t['priority']} · Category: {_label(t['category'])}",
             f"Raised by {t['raised_by']} on {fmt_date(t['created_at'][:10])}"
             + (f" · Assigned to {t['assignee_id']}" if t.get("assignee_id") else " · Not yet assigned"),
             f"Details: {t['description']}"]
    shown = [e for e in events if e["kind"] != "created"]
    if shown:
        lines.append("History:")
        for e in shown:
            body = _label(e["body"]) if e["kind"] in ("status", "assigned") else f"\"{e['body']}\""
            lines.append(f"- {fmt_date(e['created_at'][:10])}, {e['author_id']} {_EVENT_TEXT.get(e['kind'], e['kind'])}: "
                         f"{body}")
    return "\n".join(lines)


def ticket_create_proposal(result: ToolResult) -> str:
    a = result.args
    return ("I'll raise this HR support ticket for you:\n"
            f"- Subject: {a['subject']}\n- Category: {_label(a['category'])}\n- Priority: {a['priority']}\n"
            f"- Details: {a['description']}\n"
            "Reply \"yes\" to submit it, or tell me what to change.")


def ticket_created(result: ToolResult) -> str:
    if result.status == "error":
        return f"I couldn't raise that ticket: {result.error}"
    t = result.data["ticket"]
    return (f"Done. Your HR support ticket {t['ticket_id']} (\"{t['subject']}\") is open with {t['priority']} "
            "priority. The HR team will pick it up; ask me for its status any time.")


def ticket_comment_proposal(result: ToolResult) -> str:
    t = result.data["ticket"]
    return (f"I'll add this comment to ticket {t['ticket_id']} (\"{t['subject']}\"):\n\"{result.args['comment']}\"\n"
            "Reply \"yes\" to add it.")


def ticket_commented(result: ToolResult) -> str:
    if result.status == "error":
        return f"I couldn't add that comment: {result.error}"
    return f"Done. Your comment was added to ticket {result.data['ticket']['ticket_id']}."


def ticket_status_proposal(result: ToolResult) -> str:
    t, a = result.data["ticket"], result.args
    note = f" with the note \"{a['note']}\"" if a.get("note") else ""
    return (f"I'll change ticket {t['ticket_id']} (\"{t['subject']}\") from {_label(result.data['previous_status'])} "
            f"to {_label(a['status'])}{note}. Reply \"yes\" to confirm.")


def ticket_status_done(result: ToolResult) -> str:
    if result.status == "error":
        return f"I couldn't update that ticket: {result.error}"
    t = result.data["ticket"]
    assigned = f" It is assigned to {t['assignee_id']}." if t.get("assignee_id") and t["status"] == "in_progress" else ""
    return f"Done. Ticket {t['ticket_id']} is now {_label(t['status'])}.{assigned}"


def ticket_rejection(result: ToolResult) -> str:
    verb = {"create_ticket": "raise that ticket", "comment_ticket": "add that comment",
            "update_ticket_status": "update that ticket"}.get(result.name, "do that")
    return f"I can't {verb}: {result.error}"


def ticket_choices(rows: list) -> str:
    return "\n".join(f"- {t.ticket_id}: {t.subject} ({_label(t.status)})" for t in rows)
