"""Working-day calculation from the holiday calendar that applies to the requester.

The calendar is chosen with the Section 5 rules: readable by the requester,
for their region (or GLOBAL), in effect, and covering the leave year; ties are
broken by scope, then recency.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from app.db.repositories import Employee
from app.ingestion.models import DocMeta
from app.policy.eligibility import is_for_region, is_readable
from app.policy.precedence import scope_rank


def choose_calendars(
    docs: Iterable[DocMeta],
    holidays_by_doc: dict[str, list[date]],
    requester: Employee,
    year: int,
    reference_date: date,
) -> list[DocMeta]:
    candidates = [
        d for d in docs
        if d.doc_type == "calendar" and is_readable(d, requester) and is_for_region(d, requester)
        and d.effective_date <= reference_date
        and any(h.year == year for h in holidays_by_doc.get(d.doc_id, []))
    ]
    if not candidates:
        return []
    best_scope = max(scope_rank(d) for d in candidates)
    candidates = [d for d in candidates if scope_rank(d) == best_scope]
    latest = max(d.effective_date for d in candidates)
    # Ties on both scope and recency are unresolved: honour every tied calendar's closures.
    return sorted((d for d in candidates if d.effective_date == latest), key=lambda d: d.doc_id)


def holiday_set(calendars: list[DocMeta], holidays_by_doc: dict[str, list[date]], year: int) -> set[date]:
    return {h for d in calendars for h in holidays_by_doc.get(d.doc_id, []) if h.year == year}


def working_days(start: date, end: date, holidays: set[date]) -> int:
    if end < start:
        return 0
    n, day = 0, start
    while day <= end:
        if day.weekday() < 5 and day not in holidays:
            n += 1
        day += timedelta(days=1)
    return n
