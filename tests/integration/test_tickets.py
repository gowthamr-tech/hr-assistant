"""Multi-agent routing to the ticket agent over /chat, and the /tickets REST API."""
from __future__ import annotations

import pytest

from app.db import repositories as repo
from app.db.database import connect
from app.orchestration import responses as R
from app.state import STATE
from app.tools import ticket_tools as tt
from tests.fakes import plan

CREATE = plan("ticket", ticket={"type": "create", "category": "payroll", "subject": "Overtime missing from payslip",
                                "description": "My October payslip is missing 6 hours of overtime."})
LIST = plan("ticket", ticket={"type": "list", "scope": "mine"})
QUEUE = plan("ticket", ticket={"type": "list", "scope": "queue"})
VOL = plan(search_query="How many volunteer days do employees get per year?")
UNKNOWN = plan(search_query="What is the pet insurance policy?")


def db():
    return connect(STATE.settings.db_path)


def tickets_of(emp: str):
    c = db()
    try:
        return repo.list_tickets(c, employee_id=emp)
    finally:
        c.close()


# ------------------------------------------------------------------ chat: supervisor -> ticket agent
def test_create_ticket_needs_confirmation(chat_factory):
    chat = chat_factory({"raise a ticket": CREATE})
    body = chat.send("E201", "Please raise a ticket, my overtime is missing", session="t1")
    assert body["route"] == "tool" and body["pending_confirmation"] is True
    assert body["tool_calls"] == [{"name": "create_ticket", "status": "pending_confirmation", "args": {
        "category": "payroll", "priority": "normal", "subject": "Overtime missing from payslip",
        "description": "My October payslip is missing 6 hours of overtime."}}]
    assert "Reply \"yes\"" in body["answer"]
    assert tickets_of("E201") == []  # nothing filed yet

    done = chat.send("E201", "yes", session="t1")
    assert done["route"] == "tool" and done["tool_calls"][0]["status"] == "success"
    [t] = tickets_of("E201")
    assert t.ticket_id in done["answer"] and t.status == "open" and t.priority == "normal"


def test_rejecting_the_proposal_files_nothing(chat_factory):
    chat = chat_factory({"raise a ticket": CREATE})
    chat.send("E201", "raise a ticket about overtime", session="t2")
    assert chat.send("E201", "no", session="t2")["answer"] == R.PROPOSAL_DISCARDED
    assert tickets_of("E201") == []


def test_list_view_and_close_own_ticket(chat_factory):
    chat = chat_factory({"raise a ticket": CREATE, "my tickets": LIST,
                         "status of": plan("ticket", ticket={"type": "view"}),
                         "close it": plan("ticket", ticket={"type": "update_status", "status": "closed"})})
    chat.send("E201", "raise a ticket", session="t3")
    chat.send("E201", "yes", session="t3")
    [t] = tickets_of("E201")

    body = chat.send("E201", "show my tickets")
    assert body["route"] == "tool" and t.ticket_id in body["answer"] and "1 ticket" in body["answer"]
    # No ticket ID given: the only open ticket is used.
    view = chat.send("E201", "what's the status of my ticket?")
    assert "Status: open" in view["answer"] and view["tool_calls"][0]["name"] == "get_ticket"

    close = chat.send("E201", "close it please", session="t4")
    assert close["pending_confirmation"] and close["tool_calls"][0]["args"]["status"] == "closed"
    assert chat.send("E201", "yes", session="t4")["tool_calls"][0]["status"] == "success"
    assert tickets_of("E201")[0].status == "closed"


def test_other_employees_ticket_is_not_found(chat_factory):
    chat = chat_factory({"raise a ticket": CREATE})
    chat.send("E201", "raise a ticket", session="t5")
    chat.send("E201", "yes", session="t5")
    tid = tickets_of("E201")[0].ticket_id
    chat = chat_factory({"ticket": plan("ticket", ticket={"type": "view", "ticket_id": tid})})
    for who in ("E202", "E200"):  # a colleague, and the employee's own manager
        body = chat.send(who, f"show ticket {tid}")
        assert body["answer"] == tt.TICKET_NOT_FOUND and body["tool_calls"][0]["status"] == "error"


def test_queue_is_hr_only_and_hr_can_resolve(chat_factory):
    chat = chat_factory({"raise a ticket": CREATE, "queue": QUEUE})
    chat.send("E201", "raise a ticket", session="t6")
    chat.send("E201", "yes", session="t6")
    tid = tickets_of("E201")[0].ticket_id

    refused = chat.send("E200", "show me the ticket queue")
    assert refused["route"] == "refuse" and refused["answer"] == tt.QUEUE_HR_ONLY

    queue = chat.send("E100", "show me the ticket queue")
    assert queue["route"] == "tool" and tid in queue["answer"] and "raised by E201" in queue["answer"]

    chat = chat_factory({"resolve": plan("ticket", ticket={
        "type": "update_status", "ticket_id": tid, "status": "resolved", "comment": "Added to November payroll."})})
    proposal = chat.send("E100", f"resolve {tid}", session="t7")
    assert proposal["pending_confirmation"] and "Added to November payroll." in proposal["answer"]
    assert chat.send("E100", "yes", session="t7")["tool_calls"][0]["status"] == "success"
    assert tickets_of("E201")[0].status == "resolved"


def test_employee_cannot_resolve_via_chat(chat_factory):
    chat = chat_factory({"raise a ticket": CREATE})
    chat.send("E201", "raise a ticket", session="t8")
    chat.send("E201", "yes", session="t8")
    chat = chat_factory({"resolved": plan("ticket", ticket={"type": "update_status", "status": "resolved"})})
    body = chat.send("E201", "mark my ticket resolved")
    assert body["route"] == "tool" and body["tool_calls"][0]["status"] == "error"
    assert not body["pending_confirmation"] and "Only the HR team" in body["answer"]


def test_create_without_details_asks(chat_factory):
    chat = chat_factory({"ticket": plan("ticket", ticket={"type": "create"})})
    body = chat.send("E201", "I want to raise a ticket")
    assert body["route"] == "clarify" and body["answer"] == R.TICKET_NEED_DETAILS


def test_no_answer_offers_a_ticket_and_policy_still_routes(chat_factory):
    chat = chat_factory({"pet insurance": UNKNOWN, "volunteer": VOL})
    body = chat.send("E201", "Do we have pet insurance?")
    assert body["route"] == "no_answer" and "support ticket" in body["answer"]
    assert chat.send("E201", "How many volunteer days do I get?")["route"] == "rag"


def test_confirming_a_ticket_keeps_working_after_a_leave_flow(chat_factory):
    """Each pending proposal goes back to the agent that owns its tool."""
    chat = chat_factory({"annual leave": plan("leave_action", needs_documents=False, action={
        "type": "apply_leave", "leave_type": "annual", "start_date": "2026-10-26", "end_date": "2026-10-27"}),
        "raise a ticket": CREATE})
    leave = chat.send("E201", "annual leave 26-27 Oct", session="t9")
    assert leave["tool_calls"][0]["name"] == "apply_leave" and leave["pending_confirmation"]
    ticket = chat.send("E201", "actually, raise a ticket about my overtime", session="t9")
    assert ticket["tool_calls"][0]["name"] == "create_ticket"
    done = chat.send("E201", "yes", session="t9")
    assert done["tool_calls"][0]["name"] == "create_ticket" and done["tool_calls"][0]["status"] == "success"


# ------------------------------------------------------------------ REST API
def api(chat, method, path, emp="E201", **kw):
    return chat.client.request(method, path, headers={"X-Employee-Id": emp}, **kw)


def test_api_lifecycle(chat_factory):
    chat = chat_factory({})
    r = api(chat, "POST", "/tickets", json={"subject": "Experience letter", "description": "Need a letter for a visa.",
                                             "category": "documents", "priority": "high"})
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    tid = r.json()["ticket"]["ticket_id"]

    assert api(chat, "GET", "/tickets").json()["tickets"][0]["ticket_id"] == tid
    assert api(chat, "GET", f"/tickets/{tid}").json()["ticket"]["description"] == "Need a letter for a visa."
    assert api(chat, "GET", f"/tickets/{tid}", emp="E202").status_code == 404
    assert api(chat, "GET", "/tickets?scope=queue").status_code == 403
    assert api(chat, "GET", "/tickets?scope=queue", emp="E100").json()["tickets"][0]["ticket_id"] == tid

    assert api(chat, "PATCH", f"/tickets/{tid}", json={"status": "resolved"}).status_code == 409
    r = api(chat, "PATCH", f"/tickets/{tid}", emp="E100", json={"status": "in_progress", "note": "On it."})
    assert r.status_code == 200 and r.json()["ticket"]["assignee_id"] == "E100"
    assert api(chat, "POST", f"/tickets/{tid}/comments", json={"comment": "Thanks!"}).status_code == 201
    events = api(chat, "GET", f"/tickets/{tid}", emp="E100").json()["events"]
    assert [e["body"] for e in events if e["kind"] == "comment"] == ["On it.", "Thanks!"]


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/tickets", {"subject": 5, "description": "x"}),
    ("POST", "/tickets", {"subject": "s", "description": "d", "category": "salary"}),
    ("PATCH", "/tickets/TKT-00000000", {"note": "x"}),
    ("POST", "/tickets/TKT-00000000/comments", {"comment": ""}),
])
def test_api_bad_requests(chat_factory, method, path, body):
    assert api(chat_factory({}), method, path, json=body).status_code == 400


def test_api_auth_and_ids(chat_factory):
    chat = chat_factory({})
    assert chat.client.get("/tickets").status_code == 401
    assert api(chat, "GET", "/tickets", emp="E999").status_code == 401
    assert api(chat, "GET", "/tickets/TKT-00000000").status_code == 404
    assert api(chat, "GET", "/tickets/xTKT-00000000x").status_code == 404
