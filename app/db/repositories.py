"""Data access. Every query is parameterised; no SQL is built from user input."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
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


def take_pending(conn: sqlite3.Connection, session_id: str, employee_id: str) -> Optional[dict]:
    """Atomically remove and return the session's pending proposal (one use only)."""
    row = conn.execute(
        "DELETE FROM assistant_pending_actions WHERE session_id = ? RETURNING employee_id, tool_name, args_json",
        (session_id,),
    ).fetchone()
    if row is None or row["employee_id"] != employee_id:
        return None
    return {"tool_name": row["tool_name"], "args": json.loads(row["args_json"])}


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
