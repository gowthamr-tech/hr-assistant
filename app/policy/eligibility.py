"""Section 5.1 eligibility: which documents a requester may use on a reference date."""
from __future__ import annotations

from datetime import date
from typing import Iterable

from app.db.repositories import Employee
from app.ingestion.models import DocMeta
from app.security.access import can_read_access_level


def is_readable(doc: DocMeta, requester: Employee) -> bool:
    return can_read_access_level(requester.role, doc.access_level)


def is_for_region(doc: DocMeta, requester: Employee) -> bool:
    return doc.applies_to_region(requester.region)


def eligible_docs(docs: Iterable[DocMeta], requester: Employee, reference_date: date) -> list[DocMeta]:
    """Readable, for the requester's region (or GLOBAL), and in effect on the reference date."""
    return [d for d in docs if is_readable(d, requester) and is_for_region(d, requester)
            and d.effective_date <= reference_date]


def upcoming_docs(docs: Iterable[DocMeta], requester: Employee, reference_date: date) -> list[DocMeta]:
    """Readable and for the requester's region, but not yet in effect: candidates for 'upcoming change'."""
    return [d for d in docs if is_readable(d, requester) and is_for_region(d, requester)
            and d.effective_date > reference_date]
