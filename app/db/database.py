"""SQLite connection handling and the tables this service adds.

The assessment tables (employees, leave_balances, leave_requests) are never
altered. Everything this service needs is in new tables prefixed `assistant_`.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

REQUIRED_TABLES = ("employees", "leave_balances", "leave_requests")

_ADDED_SCHEMA = """
CREATE TABLE IF NOT EXISTS assistant_sessions (
    session_id   TEXT PRIMARY KEY,
    employee_id  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assistant_turns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assistant_turns_session ON assistant_turns(session_id, id);
CREATE TABLE IF NOT EXISTS assistant_pending_actions (
    session_id   TEXT PRIMARY KEY,
    employee_id  TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    args_json    TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assistant_holidays (
    doc_id       TEXT NOT NULL,
    holiday_date TEXT NOT NULL,
    name         TEXT NOT NULL,
    PRIMARY KEY (doc_id, holiday_date, name)
);
CREATE INDEX IF NOT EXISTS idx_leave_requests_employee ON leave_requests(employee_id, status);
CREATE INDEX IF NOT EXISTS idx_employees_manager ON employees(manager_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    # isolation_level=None: we issue BEGIN/COMMIT ourselves so write transactions are explicit.
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connection(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE takes the write lock up front, so check-then-write sequences
    (balance check, overlap check, insert, deduct) cannot interleave between requests."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def verify_and_migrate(db_path: str) -> None:
    """Check the assessment schema exists and add this service's own tables."""
    with connection(db_path) as conn:
        present = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in REQUIRED_TABLES if t not in present]
        if missing:
            raise RuntimeError(f"Database is missing required tables: {missing}")
        conn.executescript(_ADDED_SCHEMA)
