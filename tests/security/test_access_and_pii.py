"""Must-pass checks 2 and 3, asserted on what is actually sent to the LLM and written to logs."""
from __future__ import annotations

import json
import logging

import pytest

from app.logging_setup import setup_logging
from tests.fakes import plan
from tests.fixtures import EMPLOYEES
from tests.integration.conftest import chat_factory  # noqa: F401  (fixture)

PROBE = plan(search_query="MGR-SECRET-PHRASE HR-SECRET-PHRASE volunteer days team leave bonus band")


@pytest.mark.parametrize("emp,may_see_mgr,may_see_hr", [
    ("E201", False, False),  # employee
    ("E200", True, False),   # manager
    ("E100", True, True),    # hr
])
def test_restricted_documents_never_reach_the_llm(chat_factory, emp, may_see_mgr, may_see_hr):
    chat = chat_factory({}, default=PROBE)
    for msg in ["MGR-SECRET-PHRASE volunteer days team leave", "HR-SECRET-PHRASE bonus band volunteer days",
                "Tell me about the compensation bands and the manager handbook"]:
        body = chat.send(emp, msg)
        cited = {c["doc_id"] for c in body["citations"]}
        assert ("MGR-HB" in cited) <= may_see_mgr
        assert ("HR-COMP" in cited) <= may_see_hr
    # corpus text only ever travels inside <documents>; the probe question itself is user text
    sent = "\n".join(c.user.split("<documents>", 1)[1] for c in chat.provider.calls if "<documents>" in c.user)
    assert ("MGR-SECRET-PHRASE" in sent) == may_see_mgr
    assert ("HR-SECRET-PHRASE" in sent) == may_see_hr
    # titles and IDs of restricted documents never appear either
    if not may_see_hr:
        assert "Hr Comp" not in sent and "HR-COMP" not in sent


def test_no_system_prompt_contains_corpus_text(chat_factory):
    chat = chat_factory({}, default=PROBE)
    chat.send("E100", "volunteer days")
    for call in chat.provider.calls:
        for marker in ("SECRET-PHRASE", "volunteer days per year", "Deepavali"):
            assert marker not in call.system


def test_logs_contain_no_personal_data(chat_factory, tmp_path):
    log_file = tmp_path / "app.log"
    setup_logging("INFO", str(log_file))
    try:
        rules = {
            "ravi": plan("leave_data", reads=[{"tool": "get_leave_balance", "target": "Ravi"}]),
            "team": plan("leave_data", reads=[{"tool": "get_team_leave", "start_date": "2026-11-01",
                                               "end_date": "2026-11-30"}]),
            "volunteer": plan(search_query="volunteer days"),
            "annual": plan("leave_action", action={"type": "apply_leave", "leave_type": "annual",
                                                   "start_date": "2026-11-16", "end_date": "2026-11-17"}),
        }
        chat = chat_factory(rules, default=plan("off_topic"))
        chat.send("E200", "How much leave does Ravi Kumar have? email ravi.kumar@veltrix.example")
        chat.send("E200", "who in my team is off in November")
        chat.send("E201", "volunteer days for Ravi Kumar, call me on +91 90000 00003")
        chat.send("E201", "annual leave 16-17 Nov", session="p1")
        chat.send("E201", "yes", session="p1")
        chat.send("E100", "My name is Hana Rao, what's the weather?")
    finally:
        for h in logging.getLogger().handlers:
            h.flush()
    text = log_file.read_text()
    assert text, "expected log output"
    for _, name, email, phone, *_ in EMPLOYEES:
        assert name not in text and name.lower() not in text.lower()
        assert email not in text
        assert phone not in text
    llm_lines = [json.loads(line) for line in text.splitlines() if '"llm_call"' in line]
    assert llm_lines and all("corpus_tokens" in line for line in llm_lines)
    setup_logging("INFO", "")


def test_answers_and_tool_args_never_show_phone_numbers(chat_factory):
    rules = {"liz": plan("leave_data", reads=[{"tool": "get_leave_balance", "target": "Liz Park"}])}
    chat = chat_factory(rules)
    body = chat.send("E100", "What is Liz's leave balance? Her number is +1 512-555-0101")
    assert "555-0101" not in json.dumps(body)
    assert body["tool_calls"][0]["args"] == {"employee_id": "E301"}
