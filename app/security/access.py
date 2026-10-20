"""Single source of truth for who may read which documents and whose leave data.

Every check takes the requester as loaded from the database for the
X-Employee-Id header. Nothing here reads the user's message or LLM output.
"""
from __future__ import annotations

from app.db.repositories import Employee

# Section 3.1 of the assessment.
_READABLE_ACCESS_LEVELS = {
    "employee": frozenset({"all"}),
    "manager": frozenset({"all", "managers"}),
    "hr": frozenset({"all", "managers", "hr"}),
}

# One fixed message for "not allowed" and "does not exist", so the response
# never reveals whether another employee ID is real.
EMPLOYEE_REFUSAL = "I can't share information about that employee."


def readable_access_levels(role: str) -> frozenset[str]:
    return _READABLE_ACCESS_LEVELS.get(role, frozenset({"all"}))


def can_read_access_level(role: str, access_level: str) -> bool:
    return access_level in readable_access_levels(role)


def can_view_leave_of(requester: Employee, target: Employee | None) -> bool:
    """Read access to one employee's balance and requests (Sections 3.1 and 6.1)."""
    if target is None:
        return False
    if target.employee_id == requester.employee_id:
        return True
    if requester.role == "hr":
        return True
    if requester.role == "manager" and target.manager_id == requester.employee_id:
        return True
    return False


def can_use_team_leave(requester: Employee) -> bool:
    return requester.role == "manager"
