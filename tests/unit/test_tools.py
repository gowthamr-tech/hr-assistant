"""Leave tools: business rules (6.2, 6.3) and in-tool permission checks (3.1, 6.1)."""
import threading
import uuid

import pytest

from app.db import repositories as repo
from app.db.database import connect
from app.security.access import EMPLOYEE_REFUSAL
from app.tools import leave_tools as tools
from tests.conftest import TODAY, employee


def ctx_for(conn, kb, emp_id):
    return tools.ToolContext(conn=conn, requester=employee(conn, emp_id), today=TODAY, docs=kb.docs,
                             holidays_by_doc=kb.holidays_by_doc, holiday_names=kb.holiday_names)


def balance(conn, emp, lt="annual"):
    return repo.get_balances(conn, emp, lt)[lt]


# ------------------------------------------------------------------ read permissions
@pytest.mark.parametrize("requester,target,allowed", [
    ("E201", "E201", True),    # self
    ("E201", "E202", False),   # colleague
    ("E201", "E200", False),   # own manager
    ("E200", "E201", True),    # manager -> direct report
    ("E200", "E203", False),   # manager -> someone else's report
    ("E200", "E300", False),   # manager -> another manager
    ("E100", "E301", True),    # HR -> anyone
    ("E100", "E410", True),
    ("E201", "E999", False),   # nonexistent
    ("E100", "E999", False),
])
def test_balance_permissions(conn, kb, requester, target, allowed):
    res = tools.get_leave_balance(ctx_for(conn, kb, requester), target)
    assert (res.status == "success") == allowed
    if not allowed:
        assert res.error == EMPLOYEE_REFUSAL  # identical for forbidden and nonexistent


def test_list_requests_permissions_and_filter(conn, kb):
    res = tools.list_leave_requests(ctx_for(conn, kb, "E200"), "E201", "pending")
    assert res.status == "success" and [r["request_id"] for r in res.data["requests"]] == ["req-e201-nov"]
    assert tools.list_leave_requests(ctx_for(conn, kb, "E202"), "E201").error == EMPLOYEE_REFUSAL


def test_balance_never_includes_phone(conn, kb):
    res = tools.get_leave_balance(ctx_for(conn, kb, "E100"), "E201")
    flat = repr(res.data)
    assert "+91" not in flat and "ravi.kumar@" not in flat and "r***@veltrix.example" in flat


def test_team_leave_managers_only(conn, kb):
    for emp in ("E201", "E100"):
        assert tools.get_team_leave(ctx_for(conn, kb, emp), "2026-11-01", "2026-11-30").status == "error"
    res = tools.get_team_leave(ctx_for(conn, kb, "E200"), "2026-11-01", "2026-11-30")
    assert res.status == "success"
    ids = {(r["employee_id"], r["request_id"]) for r in res.data["leave"]}
    # direct reports only, pending/approved only, overlapping only
    assert ids == {("E201", "req-e201-nov")}


def test_team_leave_overlap_edges(conn, kb):
    ctx = ctx_for(conn, kb, "E200")
    assert tools.get_team_leave(ctx, "2026-11-13", "2026-11-13").data["leave"]  # last day
    assert not tools.get_team_leave(ctx, "2026-11-14", "2026-11-20").data["leave"]
    assert tools.get_team_leave(ctx, "2026-10-01", "2026-10-31").data["leave"][0]["request_id"] == "req-e202-oct"


# ------------------------------------------------------------------ apply_leave
def test_apply_success_is_transactional(conn, kb):
    ctx = ctx_for(conn, kb, "E201")
    before = balance(conn, "E201")
    res = tools.apply_leave(ctx, "annual", "2026-11-16", "2026-11-20")
    assert res.status == "success" and res.data["working_days"] == 5
    uuid.UUID(res.data["request_id"], version=4)
    row = repo.get_request(conn, res.data["request_id"])
    assert (row.status, row.working_days, row.employee_id) == ("pending", 5, "E201")
    assert balance(conn, "E201") == before - 5


def test_dry_run_changes_nothing(conn, kb):
    ctx = ctx_for(conn, kb, "E201")
    before = (balance(conn, "E201"), len(repo.list_requests(conn, "E201")))
    assert tools.apply_leave(ctx, "annual", "2026-11-16", "2026-11-20", dry_run=True).status == "success"
    assert (balance(conn, "E201"), len(repo.list_requests(conn, "E201"))) == before


@pytest.mark.parametrize("lt,start,end,code", [
    ("vacation", "2026-11-16", "2026-11-17", "invalid_leave_type"),
    ("annual", "2027-01-04", "2027-01-05", "outside_year"),
    ("annual", "2026-12-30", "2027-01-02", "outside_year"),
    ("annual", "2026-10-15", "2026-10-19", "start_in_past"),
    ("annual", "2026-11-20", "2026-11-16", "invalid_range"),
    ("annual", "2026-11-14", "2026-11-15", "zero_working_days"),   # weekend
    ("annual", "2026-12-25", "2026-12-27", "zero_working_days"),   # holiday + weekend
    ("annual", "2026-11-13", "2026-11-17", "overlap"),             # shares 13 Nov with pending request
    ("annual", "2026-11-14", "2026-11-16", "insufficient_balance"),  # placeholder replaced below
    ("annual", "not-a-date", "2026-11-16", "invalid_date"),
])
def test_apply_rejections(conn, kb, lt, start, end, code):
    ctx = ctx_for(conn, kb, "E201")
    if code == "insufficient_balance":
        ctx = ctx_for(conn, kb, "E202")  # 2 annual days
        start, end = "2026-11-16", "2026-11-18"
    before = len(repo.list_requests(conn, ctx.requester.employee_id))
    res = tools.apply_leave(ctx, lt, start, end)
    assert res.status == "error" and res.error_code == code and res.error
    assert len(repo.list_requests(conn, ctx.requester.employee_id)) == before


def test_start_today_is_allowed(conn, kb):
    assert tools.apply_leave(ctx_for(conn, kb, "E201"), "sick", TODAY.isoformat(), TODAY.isoformat(),
                             dry_run=True).status == "success"


def test_partial_overlap_is_rejected(conn, kb):
    # existing E201 request 14-15 Dec; sharing just 15 Dec is enough to reject
    ctx = ctx_for(conn, kb, "E201")
    res = tools.apply_leave(ctx, "annual", "2026-12-15", "2026-12-16")
    assert res.error_code == "overlap"


def test_cancelled_requests_do_not_block(conn, kb):
    # E202 has a cancelled request on 2-3 Nov
    res = tools.apply_leave(ctx_for(conn, kb, "E202"), "annual", "2026-11-02", "2026-11-03", dry_run=True)
    assert res.status == "success"


def test_exact_balance_is_enough(conn, kb):
    res = tools.apply_leave(ctx_for(conn, kb, "E202"), "annual", "2026-11-16", "2026-11-17")
    assert res.status == "success" and balance(conn, "E202") == 0


def test_concurrent_applications_cannot_overdraw(db_path, kb):
    """Two simultaneous 2-day requests against a 2-day balance: exactly one wins."""
    results = []

    def worker(start, end):
        c = connect(str(db_path))
        try:
            results.append(tools.apply_leave(ctx_for(c, kb, "E202"), "annual", start, end))
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=a) for a in
               [("2026-11-16", "2026-11-17"), ("2026-11-23", "2026-11-24")]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r.status for r in results) == ["error", "success"]
    c = connect(str(db_path))
    assert balance(c, "E202") == 0
    c.close()


def test_concurrent_overlapping_applications(db_path, kb):
    results = []

    def worker():
        c = connect(str(db_path))
        try:
            results.append(tools.apply_leave(ctx_for(c, kb, "E201"), "annual", "2026-11-30", "2026-11-30"))
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [r.status for r in results].count("success") == 1


# ------------------------------------------------------------------ cancel_leave
def test_cancel_restores_balance(conn, kb):
    ctx = ctx_for(conn, kb, "E201")
    before = balance(conn, "E201")
    res = tools.cancel_leave(ctx, "req-e201-nov")
    assert res.status == "success" and res.data["restored_days"] == 4
    assert repo.get_request(conn, "req-e201-nov").status == "cancelled"
    assert balance(conn, "E201") == before + 4


def test_cancel_twice_fails_safely(conn, kb):
    ctx = ctx_for(conn, kb, "E201")
    assert tools.cancel_leave(ctx, "req-e201-nov").status == "success"
    before = balance(conn, "E201")
    second = tools.cancel_leave(ctx, "req-e201-nov")
    assert second.status == "error" and second.error_code == "not_cancellable"
    assert balance(conn, "E201") == before


@pytest.mark.parametrize("emp,request_id,code", [
    ("E201", "req-e201-feb", "already_started"),   # in the past
    ("E202", "req-e202-oct", "already_started"),   # started today
    ("E202", "req-e202-old", "not_cancellable"),   # already cancelled
    ("E202", "req-e201-nov", "not_found"),         # someone else's
    ("E200", "req-e201-nov", "not_found"),         # even their manager cannot cancel it
    ("E100", "req-e201-nov", "not_found"),         # nor HR
    ("E201", "does-not-exist", "not_found"),
])
def test_cancel_rejections(conn, kb, emp, request_id, code):
    res = tools.cancel_leave(ctx_for(conn, kb, emp), request_id)
    assert res.status == "error" and res.error_code == code


def test_not_found_message_is_identical_for_foreign_and_missing(conn, kb):
    a = tools.cancel_leave(ctx_for(conn, kb, "E202"), "req-e201-nov").error
    b = tools.cancel_leave(ctx_for(conn, kb, "E202"), "nope").error
    assert a == b == tools.REQUEST_NOT_FOUND
