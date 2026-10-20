from datetime import date

from app.ingestion.models import DocMeta
from app.policy.calendar import choose_calendars, holiday_set, working_days
from app.tools import leave_tools as tools
from tests.conftest import TODAY, employee

D = date.fromisoformat


def test_single_weekday_counts_one():
    assert working_days(D("2026-11-10"), D("2026-11-10"), set()) == 1


def test_weekend_only_is_zero():
    assert working_days(D("2026-11-14"), D("2026-11-15"), set()) == 0


def test_full_week_is_five():
    assert working_days(D("2026-11-16"), D("2026-11-20"), set()) == 5


def test_range_spanning_weekend():
    # Thu..Tue = Thu, Fri, Mon, Tue
    assert working_days(D("2026-11-19"), D("2026-11-24"), set()) == 4


def test_holiday_on_weekday_excluded():
    assert working_days(D("2026-11-09"), D("2026-11-13"), {D("2026-11-09")}) == 4


def test_holiday_on_weekend_not_double_counted():
    assert working_days(D("2026-08-14"), D("2026-08-17"), {D("2026-08-15")}) == 2


def test_range_spanning_weekend_and_holidays():
    hols = {D("2026-12-24"), D("2026-12-25"), D("2026-12-31")}
    # Mon 21 .. Thu 31 December: 9 weekdays minus three holidays
    assert working_days(D("2026-12-21"), D("2026-12-31"), hols) == 6


def test_end_before_start_is_zero():
    assert working_days(D("2026-11-20"), D("2026-11-16"), set()) == 0


def _meta(doc_id, regions, eff, doc_type="calendar", access="all"):
    return DocMeta(doc_id, f"{doc_id}.md", doc_id, doc_type, tuple(regions), D(eff), access)


def test_calendar_choice_prefers_region_and_recency(conn):
    emp = employee(conn, "E201")  # IN employee
    docs = [_meta("GLOBAL-CAL", ["GLOBAL"], "2026-01-01"), _meta("IN-OLD", ["IN"], "2025-06-01"),
            _meta("IN-NEW", ["IN"], "2026-02-01"), _meta("DE", ["DE"], "2026-01-01"),
            _meta("IN-FUTURE", ["IN"], "2026-12-01"), _meta("IN-HR", ["IN"], "2026-03-01", access="hr")]
    hols = {d.doc_id: [D("2026-05-01")] for d in docs}
    chosen = choose_calendars(docs, hols, emp, 2026, TODAY)
    assert [d.doc_id for d in chosen] == ["IN-NEW"]


def test_calendar_must_cover_the_leave_year(conn):
    emp = employee(conn, "E201")
    docs = [_meta("IN-2025", ["IN"], "2025-01-01"), _meta("IN-2026", ["IN"], "2026-01-01")]
    hols = {"IN-2025": [D("2025-10-20")], "IN-2026": [D("2026-10-20")]}
    chosen = choose_calendars(docs, hols, emp, 2026, TODAY)
    assert [d.doc_id for d in chosen] == ["IN-2026"]
    assert holiday_set(chosen, hols, 2026) == {D("2026-10-20")}


def _ctx(conn, kb, emp_id):
    return tools.ToolContext(conn=conn, requester=employee(conn, emp_id), today=TODAY, docs=kb.docs,
                             holidays_by_doc=kb.holidays_by_doc, holiday_names=kb.holiday_names)


def test_regional_calendars_from_ingested_documents(conn, kb):
    # Week of 9 Nov 2026: Deepavali (IN) only
    assert tools.count_working_days(_ctx(conn, kb, "E201"), D("2026-11-09"), D("2026-11-13"))[0] == 4
    assert tools.count_working_days(_ctx(conn, kb, "E400"), D("2026-11-09"), D("2026-11-13"))[0] == 5
    # Thanksgiving week (US) vs India
    assert tools.count_working_days(_ctx(conn, kb, "E301"), D("2026-11-23"), D("2026-11-27"))[0] == 3
    assert tools.count_working_days(_ctx(conn, kb, "E201"), D("2026-11-23"), D("2026-11-27"))[0] == 5
    # Germany: company holidays on 24 and 31 December count as closures
    assert tools.count_working_days(_ctx(conn, kb, "E400"), D("2026-12-21"), D("2026-12-31"))[0] == 6


def test_restricted_holidays_are_working_days(conn, kb):
    # 24 Nov 2026 is listed only as a restricted (optional) holiday for India
    assert tools.count_working_days(_ctx(conn, kb, "E201"), D("2026-11-24"), D("2026-11-24"))[0] == 1
