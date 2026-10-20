"""Regression tests for issues found in the adversarial review."""
from __future__ import annotations

import time

import pytest

from app.api import routes
from app.db import repositories as repo
from app.db.database import connect
from app.llm.client import schema_violation
from app.llm import prompts
from app.state import STATE
from tests.fakes import plan
from tests.integration.conftest import chat_factory  # noqa: F401


def apply(start, end, lt="annual", **kw):
    return plan("leave_action", action={"type": "apply_leave", "leave_type": lt, "start_date": start,
                                        "end_date": end, **kw})


def pending_rows():
    c = connect(STATE.settings.db_path)
    try:
        return c.execute("SELECT * FROM assistant_pending_actions").fetchall()
    finally:
        c.close()


@pytest.mark.parametrize("start,end", [("20261116", "20261117"), ("2026-W47-1", "2026-W47-2")])
def test_only_canonical_iso_dates_accepted(chat_factory, start, end):
    chat = chat_factory({"annual": apply(start, end, dates_explicit_year=True)})
    body = chat.send("E201", "annual leave please")
    assert body["route"] == "clarify" and not body["pending_confirmation"]


def test_timed_out_request_leaves_no_hidden_proposal(chat_factory, monkeypatch):
    def slow_router(message):
        time.sleep(1.0)
        return apply("2026-11-16", "2026-11-17", dates_explicit_year=True)

    chat = chat_factory({})
    chat.provider.router = slow_router
    monkeypatch.setattr(routes, "HANDLER_TIMEOUT_S", 0.3)
    first = chat.send("E201", "annual leave 16-17 Nov", session="t1")
    assert first["route"] == "clarify" and not first["pending_confirmation"]
    time.sleep(1.2)  # let the worker finish
    assert pending_rows() == []


def test_deeply_nested_body_is_400_and_unknown_header_401(chat_factory):
    c = chat_factory({}).client
    deep = b"[" * 30000
    assert c.post("/chat", content=deep, headers={"X-Employee-Id": "E201"}).status_code == 400
    assert c.post("/chat", content=deep, headers={"X-Employee-Id": "E999"}).status_code == 401


def test_lone_surrogate_is_400(chat_factory):
    c = chat_factory({}).client
    r = c.post("/chat", content=b'{"session_id": "s", "message": "hi \\ud800"}',
               headers={"X-Employee-Id": "E201", "Content-Type": "application/json"})
    assert r.status_code == 400


@pytest.mark.parametrize("bad", [
    {"category": "leave_data", "reads": 5},
    {"category": 7},
    {"category": "leave_action", "action": {"type": "apply_leave", "leave_type": ["annual"]}},
])
def test_schema_invalid_llm_output_degrades_to_clarify(chat_factory, bad):
    chat = chat_factory({}, default=bad)
    body = chat.send("E201", "anything")
    assert body["route"] == "clarify"


def test_schema_validator():
    good = plan()
    assert schema_violation(good, prompts.ROUTER_SCHEMA) is None
    assert schema_violation({**good, "reads": "x"}, prompts.ROUTER_SCHEMA)
    assert schema_violation({"claims": [{"rule": 1}], "unanswered": ""}, prompts.EXTRACT_SCHEMA)


@pytest.mark.parametrize("message,target", [
    ("Apply annual leave for Ravi Kumar on 27 Oct", "Ravi Kumar"),
    ("Apply annual leave for E201 on 27 Oct", "self"),   # interpreter missed it; code still catches the ID
])
def test_cannot_apply_for_someone_else(chat_factory, message, target):
    chat = chat_factory({"apply": apply("2026-10-27", "2026-10-27", target=target)})
    body = chat.send("E200", message)
    assert body["route"] == "refuse" and body["tool_calls"] == []


def test_no_with_new_details_makes_new_proposal(chat_factory):
    chat = chat_factory({"friday": apply("2026-11-16", "2026-11-20"), "annual": apply("2026-11-16", "2026-11-17")})
    chat.send("E201", "annual leave 16-17 Nov", session="n2")
    body = chat.send("E201", "No, make it until Friday", session="n2")
    assert body["pending_confirmation"] and body["tool_calls"][0]["args"]["end_date"] == "2026-11-20"


def test_leave_type_from_unrelated_earlier_question_is_not_used(chat_factory):
    chat = chat_factory({"policy": plan(search_query="sick leave policy"),
                         "apply": apply("2026-10-27", "2026-10-27", lt="sick")})
    chat.send("E201", "what is the sick leave policy?", session="g1")
    body = chat.send("E201", "ok, apply leave for 27 Oct", session="g1")
    assert body["route"] == "clarify" and "annual or sick" in body["answer"]


def test_leave_type_from_same_flow_is_used(chat_factory):
    chat = chat_factory({"dates": apply("2026-11-16", "2026-11-17", lt="annual"),
                         "annual": apply("", "", lt="annual", ), })
    chat.send("E201", "I want annual leave", session="f1")
    body = chat.send("E201", "dates are 16-17 Nov", session="f1")
    assert body["pending_confirmation"]


def test_unanswered_is_counted_as_corpus(chat_factory):
    chat = chat_factory({"volunteer": plan(search_query="volunteer days")})
    chat.provider.extractor = lambda user: {"claims": [{"rule": "v", "value": "3", "statement": "s", "source": "S1"}],
                                            "unanswered": "copied corpus text " * 2000}
    chat.send("E201", "volunteer days?")
    compose = next(c for c in chat.provider.calls if c.purpose == "compose_answer")
    outside = compose.user.split("<documents>")[0]
    assert "copied corpus text" not in outside


def test_interpreter_never_learns_ids_of_invisible_people(chat_factory):
    chat = chat_factory({}, default=plan("leave_data", reads=[{"tool": "get_leave_balance", "target": "Ravi Kumar"}]))
    chat.send("E203", "What is Ravi Kumar's balance?", session="h1")  # E203 cannot see E201
    chat.send("E203", "and the other one?", session="h1")
    second = [c for c in chat.provider.calls if c.purpose == "interpret"][1]
    history = second.user.split("RECENT CONVERSATION:")[1].split("LATEST MESSAGE:")[0]
    assert "E201" not in history and "Ravi" not in history


def test_stuck_llm_call_cannot_stall_the_request(chat_factory, monkeypatch):
    """A provider whose own timeout never fires must still be cut off by the client."""
    def hang(message):
        time.sleep(5)
        return plan(search_query="volunteer days")

    chat = chat_factory({})
    chat.provider.router = hang
    monkeypatch.setattr(STATE.orchestrator.llm, "timeout_s", 0.5)
    t = time.time()
    body = chat.send("E201", "volunteer days?")
    assert time.time() - t < 3 and body["route"] == "clarify"
