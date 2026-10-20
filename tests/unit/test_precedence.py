"""Section 5 rules, including the worked example of Section 5.2."""
from datetime import date

from app.db.repositories import Employee
from app.ingestion.models import DocMeta
from app.policy.eligibility import eligible_docs, upcoming_docs
from app.policy.precedence import Claim, find_upcoming, normalise_value, resolve_all, resolve_rule

D = date.fromisoformat


def meta(doc_id, doc_type, regions, eff, access="all"):
    return DocMeta(doc_id, f"{doc_id}.pdf", doc_id, doc_type, tuple(regions), D(eff), access)


V1 = meta("V1", "policy", ["GLOBAL"], "2025-01-01")
V2 = meta("V2", "policy", ["GLOBAL"], "2026-04-01")
FAQ = meta("FAQ", "faq", ["GLOBAL"], "2026-05-01")
DE = meta("DE", "addendum", ["DE"], "2025-06-01")
V3 = meta("V3", "policy", ["GLOBAL"], "2027-01-01")
ALL = [V1, V2, FAQ, DE, V3]
VALUES = {"V1": "2", "V2": "3", "FAQ": "2", "DE": "5", "V3": "4"}


def emp(region, role="employee"):
    return Employee("E1", "n", "e@x", "p", region, "2020-01-01", "L3", role, None)


def claims_for(docs):
    return [Claim("volunteer_days", VALUES[d.doc_id], "", d, "s") for d in docs]


def answer(region, ref):
    e = emp(region)
    current = eligible_docs(ALL, e, ref)
    future = upcoming_docs(ALL, e, ref)
    res = resolve_all(claims_for(current))
    assert len(res) == 1
    up = find_upcoming(claims_for(current), claims_for(future)) if ref >= D("2026-09-15") else []
    return res[0], up


def test_india_employee_today():
    res, upcoming = answer("IN", D("2026-09-15"))
    assert [c.doc.doc_id for c in res.winners] == ["V2"] and not res.conflict
    assert res.decided_by == "recency"  # FAQ drops on authority, then V2 beats V1 on recency
    assert [(u.claim.doc.doc_id, u.effective_date) for u in upcoming] == [("V3", D("2027-01-01"))]


def test_germany_employee_scope_wins_and_v3_not_mentioned():
    res, upcoming = answer("DE", D("2026-09-15"))
    assert [c.doc.doc_id for c in res.winners] == ["DE"]
    assert res.values == ["5"]
    assert upcoming == []


def test_india_employee_historical_date():
    res, _ = answer("IN", D("2025-08-01"))
    assert [c.doc.doc_id for c in res.winners] == ["V1"] and res.values == ["2"]


def test_authority_beats_recency():
    res = resolve_rule([Claim("r", "2", "", FAQ, "s"), Claim("r", "3", "", V2, "s")])
    assert [c.doc.doc_id for c in res.winners] == ["V2"] and res.decided_by == "authority"


def test_scope_beats_recency_within_authority():
    newer_global = meta("G", "policy", ["GLOBAL"], "2026-06-01")
    res = resolve_rule([Claim("r", "5", "", DE, "s"), Claim("r", "3", "", newer_global, "s")])
    assert [c.doc.doc_id for c in res.winners] == ["DE"] and res.decided_by == "scope"


def test_faq_scope_does_not_beat_global_policy():
    regional_faq = meta("RF", "faq", ["IN"], "2026-06-01")
    res = resolve_rule([Claim("r", "9", "", regional_faq, "s"), Claim("r", "3", "", V2, "s")])
    assert [c.doc.doc_id for c in res.winners] == ["V2"]


def test_recency_decides_last():
    res = resolve_rule([Claim("r", "2", "", V1, "s"), Claim("r", "3", "", V2, "s")])
    assert [c.doc.doc_id for c in res.winners] == ["V2"] and res.decided_by == "recency"


def test_unresolved_conflict_reports_both():
    a = meta("POL", "policy", ["GLOBAL"], "2026-01-01")
    b = meta("HB", "handbook", ["GLOBAL"], "2026-01-01")
    res = resolve_rule([Claim("deadline", "30 days", "", a, "s"), Claim("deadline", "45 days", "", b, "s")])
    assert res.conflict
    assert sorted(c.doc.doc_id for c in res.winners) == ["HB", "POL"]


def test_two_region_specific_documents_tie_on_scope():
    memo = meta("MEMO", "memo", ["IN", "US"], "2026-07-01")
    add = meta("ADD", "addendum", ["IN"], "2025-01-01")
    res = resolve_rule([Claim("r", "3", "", memo, "s"), Claim("r", "1", "", add, "s")])
    assert [c.doc.doc_id for c in res.winners] == ["MEMO"] and res.decided_by == "recency"


def test_agreeing_values_are_not_a_conflict():
    summary = meta("V2S", "policy", ["GLOBAL"], "2026-04-01")
    res = resolve_rule([Claim("r", "12 working days", "", V2, "s"), Claim("r", "12 days", "", summary, "s")])
    assert not res.conflict and len(res.winners) == 2


def test_value_normalisation():
    assert normalise_value("12 working days") == normalise_value("12 days per year")
    assert normalise_value("31 March") != normalise_value("30 June")
    assert normalise_value("2 days") != normalise_value("3 days")


def test_upcoming_change_for_rule_with_no_current_document():
    up = find_upcoming([], [Claim("wellbeing_days", "2", "", V3, "s")])
    assert [u.claim.doc.doc_id for u in up] == ["V3"]


def test_upcoming_change_with_same_value_is_not_mentioned():
    up = find_upcoming([Claim("sick", "12", "", V2, "s")], [Claim("sick", "12", "", V3, "s")])
    assert up == []


def test_eligibility_filters_role_region_and_date():
    docs = [meta("A", "policy", ["GLOBAL"], "2026-01-01"), meta("M", "handbook", ["GLOBAL"], "2026-01-01", "managers"),
            meta("H", "policy", ["GLOBAL"], "2026-01-01", "hr"), meta("US", "addendum", ["US"], "2026-01-01"),
            meta("F", "policy", ["GLOBAL"], "2027-01-01")]
    ids = lambda e: sorted(d.doc_id for d in eligible_docs(docs, e, D("2026-10-16")))  # noqa: E731
    assert ids(emp("IN")) == ["A"]
    assert ids(emp("IN", "manager")) == ["A", "M"]
    assert ids(emp("IN", "hr")) == ["A", "H", "M"]
    assert ids(emp("US")) == ["A", "US"]
    assert [d.doc_id for d in upcoming_docs(docs, emp("IN"), D("2026-10-16"))] == ["F"]


def test_several_values_from_one_document_are_complementary():
    table = meta("TBL", "policy", ["GLOBAL"], "2025-01-01")
    res = resolve_rule([Claim("accrual", "1.5 (under 2 years)", "", table, "s"),
                        Claim("accrual", "1.75 (2 to 5 years)", "", table, "s")])
    assert not res.conflict and len(res.winners) == 2


def test_regional_single_value_replaces_per_band_values():
    table = meta("TBL", "policy", ["GLOBAL"], "2025-01-01")
    res = resolve_rule([Claim("accrual", "1.5 (under 2 years)", "", table, "s"),
                        Claim("accrual", "1.75 (2 to 5 years)", "", table, "s"),
                        Claim("accrual", "2.5 per month", "", DE, "s")])
    assert [c.doc.doc_id for c in res.winners] == ["DE"] and res.decided_by == "scope"


def test_upcoming_reports_every_changed_band():
    table = meta("TBL", "policy", ["GLOBAL"], "2025-01-01")
    current = [Claim("accrual", "1.5 (under 2 years)", "", table, "s"),
               Claim("accrual", "1.75 (2 to 5 years)", "", table, "s"),
               Claim("accrual", "2.0 (5+ years)", "", table, "s")]
    future = [Claim("accrual", "1.5 (under 2 years)", "", V3, "s"),
              Claim("accrual", "2.0 (2 to 5 years)", "", V3, "s"),
              Claim("accrual", "2.25 (5+ years)", "", V3, "s")]
    up = find_upcoming(current, future)
    assert sorted(u.claim.value for u in up) == ["2.0 (2 to 5 years)", "2.25 (5+ years)"]


def test_same_wording_is_not_a_conflict():
    summary = meta("V2S", "policy", ["GLOBAL"], "2026-04-01")
    res = resolve_rule([Claim("accrual_basis", "earned each month", "", V2, "s"),
                        Claim("accrual_basis", "Earned monthly", "", summary, "s")])
    assert not res.conflict


def test_negation_is_a_conflict():
    a = meta("POL", "policy", ["GLOBAL"], "2026-01-01")
    b = meta("HB", "handbook", ["GLOBAL"], "2026-01-01")
    res = resolve_rule([Claim("half_days", "allowed", "", a, "s"), Claim("half_days", "not allowed", "", b, "s")])
    assert res.conflict
