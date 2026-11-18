"""Data access. Every query is parameterised; no SQL is built from user input."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional


@dataclass(frozen=True)
class Employee:
    employee_id: str
    full_name: str
    email: str
    phone: str
    region: str
    join_date: str
    level: str
    role: str
    manager_id: Optional[str]


@dataclass(frozen=True)
class LeaveRequest:
    request_id: str
    employee_id: str
    leave_type: str
    start_date: str
    end_date: str
    working_days: int
    status: str
    created_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _employee(row: sqlite3.Row | None) -> Optional[Employee]:
    if row is None:
        return None
    return Employee(**{k: row[k] for k in row.keys()})


def _request(row: sqlite3.Row) -> LeaveRequest:
    return LeaveRequest(**{k: row[k] for k in row.keys()})


# ---------------------------------------------------------------- employees
def get_employee(conn: sqlite3.Connection, employee_id: str) -> Optional[Employee]:
    row = conn.execute(
        "SELECT employee_id, full_name, email, phone, region, join_date, level, role, manager_id "
        "FROM employees WHERE employee_id = ?",
        (employee_id,),
    ).fetchone()
    return _employee(row)


def get_direct_reports(conn: sqlite3.Connection, manager_id: str) -> list[Employee]:
    rows = conn.execute(
        "SELECT employee_id, full_name, email, phone, region, join_date, level, role, manager_id "
        "FROM employees WHERE manager_id = ? ORDER BY employee_id",
        (manager_id,),
    ).fetchall()
    return [_employee(r) for r in rows]  # type: ignore[misc]


def list_all_employees(conn: sqlite3.Connection) -> list[Employee]:
    rows = conn.execute(
        "SELECT employee_id, full_name, email, phone, region, join_date, level, role, manager_id "
        "FROM employees ORDER BY employee_id"
    ).fetchall()
    return [_employee(r) for r in rows]  # type: ignore[misc]


# ---------------------------------------------------------------- balances
def get_balances(conn: sqlite3.Connection, employee_id: str, leave_type: Optional[str] = None) -> dict[str, float]:
    if leave_type:
        rows = conn.execute(
            "SELECT leave_type, balance_days FROM leave_balances WHERE employee_id = ? AND leave_type = ?",
            (employee_id, leave_type),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT leave_type, balance_days FROM leave_balances WHERE employee_id = ? ORDER BY leave_type",
            (employee_id,),
        ).fetchall()
    return {r["leave_type"]: float(r["balance_days"]) for r in rows}


def adjust_balance(conn: sqlite3.Connection, employee_id: str, leave_type: str, delta: float) -> None:
    cur = conn.execute(
        "UPDATE leave_balances SET balance_days = balance_days + ? WHERE employee_id = ? AND leave_type = ?",
        (delta, employee_id, leave_type),
    )
    if cur.rowcount != 1:
        raise RuntimeError("balance row missing")


# ---------------------------------------------------------------- requests
def list_requests(conn: sqlite3.Connection, employee_id: str, status: Optional[str] = None) -> list[LeaveRequest]:
    sql = (
        "SELECT request_id, employee_id, leave_type, start_date, end_date, working_days, status, created_at "
        "FROM leave_requests WHERE employee_id = ?"
    )
    params: list = [employee_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY start_date, created_at"
    return [_request(r) for r in conn.execute(sql, params).fetchall()]


def get_request(conn: sqlite3.Connection, request_id: str) -> Optional[LeaveRequest]:
    row = conn.execute(
        "SELECT request_id, employee_id, leave_type, start_date, end_date, working_days, status, created_at "
        "FROM leave_requests WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    return _request(row) if row else None


def active_requests_overlapping(
    conn: sqlite3.Connection, employee_ids: Iterable[str], start: date, end: date
) -> list[LeaveRequest]:
    ids = list(employee_ids)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT request_id, employee_id, leave_type, start_date, end_date, working_days, status, created_at "
        f"FROM leave_requests WHERE employee_id IN ({placeholders}) "
        "AND status IN ('pending', 'approved') AND start_date <= ? AND end_date >= ? "
        "ORDER BY start_date, employee_id",
        (*ids, end.isoformat(), start.isoformat()),
    ).fetchall()
    return [_request(r) for r in rows]


def insert_request(conn: sqlite3.Connection, req: LeaveRequest) -> None:
    conn.execute(
        "INSERT INTO leave_requests (request_id, employee_id, leave_type, start_date, end_date, working_days, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (req.request_id, req.employee_id, req.leave_type, req.start_date, req.end_date,
         req.working_days, req.status, req.created_at),
    )


def set_request_status(conn: sqlite3.Connection, request_id: str, from_statuses: tuple[str, ...], to_status: str) -> bool:
    """Conditional update: only succeeds if the row is still in one of `from_statuses`."""
    placeholders = ",".join("?" for _ in from_statuses)
    cur = conn.execute(
        f"UPDATE leave_requests SET status = ? WHERE request_id = ? AND status IN ({placeholders})",
        (to_status, request_id, *from_statuses),
    )
    return cur.rowcount == 1


# ---------------------------------------------------------------- sessions
def claim_session(conn: sqlite3.Connection, session_id: str, employee_id: str) -> str:
    """Bind a session to its first employee; returns the owner."""
    conn.execute(
        "INSERT OR IGNORE INTO assistant_sessions (session_id, employee_id, created_at) VALUES (?, ?, ?)",
        (session_id, employee_id, _now_iso()),
    )
    row = conn.execute("SELECT employee_id FROM assistant_sessions WHERE session_id = ?", (session_id,)).fetchone()
    return row["employee_id"]


def add_turn(conn: sqlite3.Connection, session_id: str, role: str, content: str) -> None:
    conn.execute(
        "INSERT INTO assistant_turns (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (session_id, role, content, _now_iso()),
    )


def recent_turns(conn: sqlite3.Connection, session_id: str, limit: int = 6) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM assistant_turns WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


# ---------------------------------------------------------------- pending actions
def set_pending(conn: sqlite3.Connection, session_id: str, employee_id: str, tool_name: str, args: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO assistant_pending_actions (session_id, employee_id, tool_name, args_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, employee_id, tool_name, json.dumps(args, sort_keys=True), _now_iso()),
    )


def take_pending(conn: sqlite3.Connection, session_id: str, employee_id: str,
                 max_age_minutes: Optional[int] = None) -> Optional[dict]:
    """Atomically remove and return the session's pending proposal (one use only).

    A proposal older than ``max_age_minutes`` is removed but not returned: a "yes"
    long after the proposal was shown must not apply it."""
    row = conn.execute(
        "DELETE FROM assistant_pending_actions WHERE session_id = ? "
        "RETURNING employee_id, tool_name, args_json, created_at",
        (session_id,),
    ).fetchone()
    if row is None or row["employee_id"] != employee_id:
        return None
    if max_age_minutes is not None:
        try:
            created = datetime.fromisoformat(row["created_at"])
        except (TypeError, ValueError):
            return None
        if datetime.now(timezone.utc) - created > timedelta(minutes=max_age_minutes):
            return None
    return {"tool_name": row["tool_name"], "args": json.loads(row["args_json"])}


# ---------------------------------------------------------------- retention
def purge_expired(conn: sqlite3.Connection, retention_days: int, pending_ttl_minutes: int) -> dict[str, int]:
    """Delete chat history and sessions idle for longer than ``retention_days`` (0 keeps them),
    and proposals older than ``pending_ttl_minutes``. Leave data itself is never touched."""
    now = datetime.now(timezone.utc)
    pending_cutoff = (now - timedelta(minutes=pending_ttl_minutes)).replace(microsecond=0).isoformat()
    counts = {"turns": 0, "sessions": 0, "pending": 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        counts["pending"] = conn.execute(
            "DELETE FROM assistant_pending_actions WHERE created_at < ?", (pending_cutoff,)).rowcount
        if retention_days > 0:
            cutoff = (now - timedelta(days=retention_days)).replace(microsecond=0).isoformat()
            counts["turns"] = conn.execute(
                "DELETE FROM assistant_turns WHERE created_at < ?", (cutoff,)).rowcount
            # A session is kept while it has any recent turn, so its owner binding survives.
            counts["sessions"] = conn.execute(
                "DELETE FROM assistant_sessions WHERE created_at < ? AND session_id NOT IN "
                "(SELECT DISTINCT session_id FROM assistant_turns)", (cutoff,)).rowcount
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return counts


# ---------------------------------------------------------------- support tickets
@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    employee_id: str
    category: str
    priority: str
    subject: str
    description: str
    status: str
    assignee_id: Optional[str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TicketEvent:
    ticket_id: str
    author_id: str
    kind: str  # "created" | "comment" | "status" | "assigned"
    body: str
    created_at: str


_TICKET_COLS = ("ticket_id, employee_id, category, priority, subject, description, status, assignee_id, "
                "created_at, updated_at")
_PRIORITY_ORDER = "CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END"
_UNCHANGED = object()


def _ticket(row: sqlite3.Row) -> Ticket:
    return Ticket(**{k: row[k] for k in row.keys()})


def insert_ticket(conn: sqlite3.Connection, t: Ticket) -> None:
    conn.execute(
        f"INSERT INTO assistant_tickets ({_TICKET_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (t.ticket_id, t.employee_id, t.category, t.priority, t.subject, t.description, t.status, t.assignee_id,
         t.created_at, t.updated_at),
    )


def get_ticket(conn: sqlite3.Connection, ticket_id: str) -> Optional[Ticket]:
    row = conn.execute(f"SELECT {_TICKET_COLS} FROM assistant_tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
    return _ticket(row) if row else None


def list_tickets(conn: sqlite3.Connection, *, employee_id: Optional[str] = None, assignee_id: Optional[str] = None,
                 statuses: Optional[Iterable[str]] = None, limit: int = 50) -> list[Ticket]:
    sql = f"SELECT {_TICKET_COLS} FROM assistant_tickets WHERE 1 = 1"
    params: list = []
    if employee_id is not None:
        sql += " AND employee_id = ?"
        params.append(employee_id)
    if assignee_id is not None:
        sql += " AND assignee_id = ?"
        params.append(assignee_id)
    status_list = list(statuses or [])
    if status_list:
        sql += f" AND status IN ({','.join('?' for _ in status_list)})"
        params.extend(status_list)
    sql += f" ORDER BY {_PRIORITY_ORDER}, created_at, ticket_id LIMIT ?"
    params.append(limit)
    return [_ticket(r) for r in conn.execute(sql, params).fetchall()]


def count_tickets(conn: sqlite3.Connection, employee_id: str, statuses: Iterable[str]) -> int:
    status_list = list(statuses)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM assistant_tickets WHERE employee_id = ? "
        f"AND status IN ({','.join('?' for _ in status_list)})",
        (employee_id, *status_list),
    ).fetchone()
    return int(row["n"])


def update_ticket(conn: sqlite3.Connection, ticket_id: str, from_statuses: tuple[str, ...], *, status: str,
                  assignee_id=_UNCHANGED) -> bool:
    """Conditional update: only succeeds if the ticket is still in one of `from_statuses`."""
    sets, params = ["status = ?", "updated_at = ?"], [status, _now_iso()]
    if assignee_id is not _UNCHANGED:
        sets.append("assignee_id = ?")
        params.append(assignee_id)
    placeholders = ",".join("?" for _ in from_statuses)
    cur = conn.execute(
        f"UPDATE assistant_tickets SET {', '.join(sets)} WHERE ticket_id = ? AND status IN ({placeholders})",
        (*params, ticket_id, *from_statuses),
    )
    return cur.rowcount == 1


def touch_ticket(conn: sqlite3.Connection, ticket_id: str) -> None:
    conn.execute("UPDATE assistant_tickets SET updated_at = ? WHERE ticket_id = ?", (_now_iso(), ticket_id))


def add_ticket_event(conn: sqlite3.Connection, ticket_id: str, author_id: str, kind: str, body: str) -> None:
    conn.execute(
        "INSERT INTO assistant_ticket_events (ticket_id, author_id, kind, body, created_at) VALUES (?, ?, ?, ?, ?)",
        (ticket_id, author_id, kind, body, _now_iso()),
    )


def ticket_events(conn: sqlite3.Connection, ticket_id: str, limit: int = 20) -> list[TicketEvent]:
    """The most recent `limit` events, oldest first."""
    rows = conn.execute(
        "SELECT ticket_id, author_id, kind, body, created_at FROM assistant_ticket_events WHERE ticket_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (ticket_id, limit),
    ).fetchall()
    return [TicketEvent(**{k: r[k] for k in r.keys()}) for r in reversed(rows)]


# ---------------------------------------------------------------- holidays
def replace_holidays(conn: sqlite3.Connection, rows: list[tuple[str, str, str]]) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM assistant_holidays")
        conn.executemany(
            "INSERT OR IGNORE INTO assistant_holidays (doc_id, holiday_date, name) VALUES (?, ?, ?)", rows
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def holidays_for_doc(conn: sqlite3.Connection, doc_id: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT holiday_date, name FROM assistant_holidays WHERE doc_id = ? ORDER BY holiday_date", (doc_id,)
    ).fetchall()
    return [(r["holiday_date"], r["name"]) for r in rows]
