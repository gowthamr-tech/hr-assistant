from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app, initialise
from app.state import STATE
from tests.fakes import ScriptedRouter, make_client

ROUTES = {"rag", "tool", "hybrid", "clarify", "no_answer", "refuse"}


def check_contract(body: dict) -> None:
    """Section 8.1 / 9.1 invariants that every response must satisfy."""
    assert set(body) == {"request_id", "answer", "route", "citations", "tool_calls", "pending_confirmation"}
    assert body["route"] in ROUTES and isinstance(body["answer"], str) and body["answer"]
    cites, calls = body["citations"], body["tool_calls"]
    for c in cites:
        assert set(c) == {"doc_id", "locator"} and c["doc_id"] and c["locator"]
    for t in calls:
        assert set(t) == {"name", "args", "status"}
        assert t["status"] in {"pending_confirmation", "success", "error"}
    route = body["route"]
    if route == "rag":
        assert cites and not calls
    elif route == "tool":
        assert calls and not cites
    elif route == "hybrid":
        assert cites and calls
    else:
        assert not cites and not calls
    assert body["pending_confirmation"] == any(t["status"] == "pending_confirmation" for t in calls)


class Chat:
    def __init__(self, client: TestClient, provider):
        self.client = client
        self.provider = provider

    def send(self, emp: str, message: str, session: str | None = None, check: bool = True):
        session = session or f"s-{uuid.uuid4()}"
        r = self.client.post("/chat", json={"session_id": session, "message": message},
                             headers={"X-Employee-Id": emp})
        assert r.status_code == 200, r.text
        body = r.json()
        if check:
            check_contract(body)
        return body


@pytest.fixture
def chat_factory(settings, kb):
    def make(rules: dict, default=None, **provider_kw) -> Chat:
        llm, provider = make_client(ScriptedRouter(rules, default), **provider_kw)
        initialise(settings, llm=llm, kb=kb)
        assert STATE.ready
        return Chat(TestClient(app), provider)

    yield make
    STATE.ready = False
