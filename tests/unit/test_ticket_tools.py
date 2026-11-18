"""Support ticket tools: ownership, HR queue, status rules and validation."""
from __future__ import annotations

import pytest

from app.db import repositories as repo
from app.tools import ticket_tools as tt
from app.tools.leave_tools import ToolContext
from tests.conftest import TODAY, employee


def ctx_for(conn, emp_id: str) -> ToolContext:
    return ToolContext(conn=conn, requester=employee(conn, emp_id), today=TODAY, docs={}, holidays_by_doc={})


def raise_ticket(conn, emp_id="E201", **kw) -> str:
    args = {"category": "payroll", "priority": "normal", "subject": "Overtime missing",
            "description": "My October payslip is missing 6 hours of overtime."}
    args.update(kw)
    res = tt.create_ticket(ctx_for(conn, emp_id), **args)
    assert res.status == "success", res.error
    return res.data["ticket"]["ticket_id"]


def test_create_is_owned_by_requester_and_logged(conn):
    tid = raise_ticket(conn)
    assert tt.TICKET_ID_RE.fullmatch(tid)
    t = repo.get_ticket(conn, tid)
    assert (t.employee_id, t.status, t.assignee_id, t.category) == ("E201", "open", None, "payroll")
    assert [e.kind for e in repo.ticket_events(conn, tid)] == ["created"]


def test_dry_run_writes_nothing(conn):
    res = tt.create_ticket(ctx_for(conn, "E201"), "payroll", "normal", "S", "D", dry_run=True)
    assert res.status == "success"
    assert repo.list_tickets(conn, employee_id="E201") == []


@pytest.mark.parametrize("kw,code", [
    ({"category": "salary"}, "invalid_category"),
    ({"priority": "p0"}, "invalid_priority"),
    ({"subject": "   "}, "missing_subject"),
    ({"description": ""}, "missing_description"),
])
def test_create_validation(conn, kw, code):
    args = {"category": "payroll", "priority": "normal", "subject": "S", "description": "D", **kw}
    res = tt.create_ticket(ctx_for(conn, "E201"), **args)
    assert res.status == "error" and res.error_code == code


def test_text_is_cleaned_and_capped(conn):
    res = tt.create_ticket(ctx_for(conn, "E201"), "other", "low", "a\x00b" + "x" * 500, "line1\nline2\x07")
    t = res.data["ticket"]
    assert "\x00" not in t["subject"] and len(t["subject"]) <= tt.MAX_SUBJECT
    assert t["description"] == "line1\nline2"


def test_open_ticket_cap(conn, monkeypatch):
    monkeypatch.setattr(tt, "MAX_ACTIVE_PER_EMPLOYEE", 2)
    raise_ticket(conn)
    raise_ticket(conn)
    res = tt.create_ticket(ctx_for(conn, "E201"), "other", "normal", "S", "D")
    assert res.error_code == "too_many_open"


def test_visibility_owner_and_hr_only(conn):
    tid = raise_ticket(conn, "E201")
    assert tt.get_ticket(ctx_for(conn, "E201"), tid).status == "success"
    assert tt.get_ticket(ctx_for(conn, "E100"), tid).status == "success"  # HR
    # Their own manager does not see it, nor does a colleague: same answer as a missing ID.
    for other in ("E200", "E202"):
        res = tt.get_ticket(ctx_for(conn, other), tid)
        assert res.error_code == "not_found" and res.error == tt.TICKET_NOT_FOUND
    assert tt.get_ticket(ctx_for(conn, "E201"), "TKT-00000000").error == tt.TICKET_NOT_FOUND


def test_lowercase_id_is_accepted(conn):
    tid = raise_ticket(conn)
    assert tt.get_ticket(ctx_for(conn, "E201"), tid.lower()).status == "success"


def test_list_scopes(conn):
    a = raise_ticket(conn, "E201", priority="low")
    b = raise_ticket(conn, "E202", priority="urgent")
    mine = tt.list_tickets(ctx_for(conn, "E201"), "mine")
    assert [t["ticket_id"] for t in mine.data["tickets"]] == [a]
    queue = tt.list_tickets(ctx_for(conn, "E100"), "queue")
    assert [t["ticket_id"] for t in queue.data["tickets"]] == [b, a]  # urgent first
    for who in ("E201", "E200"):
        assert tt.list_tickets(ctx_for(conn, who), "queue").error_code == "forbidden"
        assert tt.list_tickets(ctx_for(conn, who), "assigned").error_code == "forbidden"
    assert tt.list_tickets(ctx_for(conn, "E100"), "assigned").data["tickets"] == []
    assert tt.list_tickets(ctx_for(conn, "E100"), "mine", "bogus").error_code == "invalid_status"


def test_hr_takes_resolves_and_queue_drops_it(conn):
    tid = raise_ticket(conn)
    hr = ctx_for(conn, "E100")
    res = tt.update_ticket_status(hr, tid, "in_progress")
    assert res.status == "success" and res.data["ticket"]["assignee_id"] == "E100"
    assert [t["ticket_id"] for t in tt.list_tickets(hr, "assigned").data["tickets"]] == [tid]
    res = tt.update_ticket_status(hr, tid, "resolved", note="Overtime added to November payroll.")
    assert res.data["ticket"]["status"] == "resolved"
    assert tt.list_tickets(hr, "queue").data["tickets"] == []
    kinds = [(e.kind, e.body) for e in repo.ticket_events(conn, tid)]
    assert ("status", "open -> in_progress") in kinds and ("assigned", "E100") in kinds
    assert ("comment", "Overtime added to November payroll.") in kinds


def test_owner_status_rights(conn):
    tid = raise_ticket(conn)
    me = ctx_for(conn, "E201")
    for status in ("in_progress", "resolved"):
        assert tt.update_ticket_status(me, tid, status).error_code == "not_allowed"
    assert tt.update_ticket_status(me, tid, "open").error_code == "unchanged"
    tt.update_ticket_status(ctx_for(conn, "E100"), tid, "resolved")
    assert tt.update_ticket_status(me, tid, "open").status == "success"  # reopen
    assert tt.update_ticket_status(me, tid, "closed").status == "success"
    # Closed is final, for HR as well.
    assert tt.update_ticket_status(ctx_for(conn, "E100"), tid, "open").error_code == "not_allowed"
    # A colleague cannot touch it at all.
    assert tt.update_ticket_status(ctx_for(conn, "E202"), tid, "closed").error_code == "not_found"


def test_comments(conn):
    tid = raise_ticket(conn)
    assert tt.comment_ticket(ctx_for(conn, "E201"), tid, "Attached the timesheet.").status == "success"
    assert tt.comment_ticket(ctx_for(conn, "E100"), tid, "Checking with payroll.").status == "success"
    assert tt.comment_ticket(ctx_for(conn, "E202"), tid, "hi").error_code == "not_found"
    assert tt.comment_ticket(ctx_for(conn, "E201"), tid, "   ").error_code == "missing_comment"
    detail = tt.get_ticket(ctx_for(conn, "E201"), tid)
    assert [e["body"] for e in detail.data["events"] if e["kind"] == "comment"] == [
        "Attached the timesheet.", "Checking with payroll."]
    tt.update_ticket_status(ctx_for(conn, "E201"), tid, "closed")
    assert tt.comment_ticket(ctx_for(conn, "E201"), tid, "one more thing").error_code == "closed"


def test_execute_revalidates(conn):
    """A proposal checked earlier must not apply if the ticket changed since."""
    tid = raise_ticket(conn)
    me = ctx_for(conn, "E201")
    assert tt.comment_ticket(me, tid, "later", dry_run=True).status == "success"
    tt.update_ticket_status(me, tid, "closed")
    assert tt.comment_ticket(me, tid, "later").error_code == "closed"
