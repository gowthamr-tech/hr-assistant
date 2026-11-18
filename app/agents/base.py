"""Types shared by the supervisor and the specialist agents.

The supervisor (``app.orchestration.orchestrator.Orchestrator``) turns a message into a ``Plan`` and
hands the turn to one specialist agent (or two, for a hybrid leave + policy answer). Every agent
returns a ``ChatResult``; none of them talks to the HTTP layer or decides who the requester is.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("app.agents")


@dataclass
class ChatResult:
    answer: str
    route: str
    citations: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    pending_confirmation: bool = False
    # A write proposal to persist. It is stored together with the turn, after the response is
    # built, so a failed or timed-out request never leaves a hidden proposal behind.
    proposal: Optional[tuple[str, dict]] = None
    # Which agent produced the answer ("supervisor", "policy", "leave", "ticket", "leave+policy").
    # Logged and counted in metrics; not part of the /chat response body.
    agent: str = "supervisor"


@dataclass
class Read:
    tool: str
    target: str = ""
    leave_type: str = ""
    status: str = ""
    start_date: str = ""
    end_date: str = ""


TICKET_FIELDS = ("type", "ticket_id", "category", "priority", "subject", "description", "comment", "status",
                 "scope")


@dataclass
class Plan:
    category: str = "hr_question"
    needs_documents: bool = False
    search_query: str = ""
    reference_date: str = ""
    reads: list[Read] = field(default_factory=list)
    action: dict = field(default_factory=dict)
    ticket: dict = field(default_factory=dict)
    date_problem: str = ""
    needs_clarification: bool = False
    clarification_question: str = ""

    @classmethod
    def from_json(cls, data: dict) -> "Plan":
        reads = []
        for r in data.get("reads") or []:
            if isinstance(r, dict) and r.get("tool") in ("get_leave_balance", "list_leave_requests", "get_team_leave"):
                reads.append(Read(**{k: str(r.get(k) or "") for k in
                                     ("tool", "target", "leave_type", "status", "start_date", "end_date")}))
        action = data.get("action") if isinstance(data.get("action"), dict) else {}
        raw_ticket = data.get("ticket") if isinstance(data.get("ticket"), dict) else {}
        ticket = {k: str(raw_ticket.get(k) or "") for k in TICKET_FIELDS}
        ticket["type"] = ticket["type"] or "none"
        return cls(
            category=str(data.get("category") or "hr_question"),
            needs_documents=bool(data.get("needs_documents")),
            search_query=str(data.get("search_query") or ""),
            reference_date=str(data.get("reference_date") or ""),
            reads=reads[:4],
            action=action,
            ticket=ticket,
            date_problem=str(data.get("date_problem") or ""),
            needs_clarification=bool(data.get("needs_clarification")),
            clarification_question=str(data.get("clarification_question") or ""),
        )


def refuse(msg: str, agent: str = "supervisor") -> ChatResult:
    log.info("refused", extra={"reason": msg, "agent": agent})
    return ChatResult(answer=msg, route="refuse", agent=agent)


def clarify(msg: str, agent: str = "supervisor") -> ChatResult:
    return ChatResult(answer=msg, route="clarify", agent=agent)
