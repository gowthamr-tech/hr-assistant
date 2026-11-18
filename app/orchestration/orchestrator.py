"""Supervisor: one message in, one specialist agent (or two) out, exactly one route.

Order of decisions for every message:
  1. A pending proposal is consumed. A clear confirmation hands it to the agent that owns the
     tool (leave or ticket) to execute; a clear "no" discards it.
  2. Deterministic guards: manipulation, bulk data, personal data -> refuse.
  3. The LLM interpreter turns the message into a plan (no corpus text in that call). Its
     category is the routing decision.
  4. The plan is handed to a specialist agent:
       hr_question               -> PolicyAgent (documents, citations)
       leave_data                -> LeaveAgent reads, plus PolicyAgent when rules are needed (hybrid)
       leave_action              -> LeaveAgent proposal
       ticket                    -> TicketAgent (support tickets)
     Agents run code: permission pre-checks, tools, retrieval, precedence, composition.
The LLM never chooses who the requester is, and never executes a tool by itself.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Optional

from app import metrics
from app.agents import people
from app.agents.base import ChatResult, Plan, Read, clarify, refuse
from app.agents.leave_agent import LeaveAgent, current_leave_thread
from app.agents.policy_agent import PolicyAgent
from app.agents.ticket_agent import TicketAgent
from app.config import Settings
from app.db import repositories as repo
from app.db.database import write_transaction
from app.db.repositories import Employee
from app.ingestion.pipeline import KnowledgeBase
from app.llm import prompts
from app.llm.client import LLMClient, LLMError
from app.orchestration import guards
from app.orchestration import responses as R
from app.orchestration.rag import RagEngine
from app.security.pii import NAMES, scrub_answer
from app.tools import leave_tools as tools

log = logging.getLogger(__name__)

# Kept importable from here for existing callers.
__all__ = ["ChatResult", "Orchestrator", "Plan", "Read", "clarify", "refuse"]
_current_leave_thread = current_leave_thread


class Orchestrator:
    """The supervisor agent. Owns routing, guards, confirmation and persistence of the turn."""

    def __init__(self, kb: KnowledgeBase, llm: Optional[LLMClient], settings: Settings) -> None:
        self.kb = kb
        self.llm = llm
        self.settings = settings
        self.rag = RagEngine(kb, llm, settings.max_corpus_tokens)
        self.policy = PolicyAgent(self.rag)
        self.leave = LeaveAgent()
        self.tickets = TicketAgent()
        self._write_owner = {name: agent for agent in (self.leave, self.tickets) for name in agent.write_tools}

    # ================================================================== entry point
    def handle(self, conn: sqlite3.Connection, requester: Employee, session_id: str, message: str,
               request_id: str, cancelled: Optional[threading.Event] = None) -> ChatResult:
        deadline = time.monotonic() + self.settings.request_deadline_s
        today = self.settings.today()
        history = repo.recent_turns(conn, session_id, limit=6)
        pending = repo.take_pending(conn, session_id, requester.employee_id,
                                    max_age_minutes=self.settings.pending_ttl_minutes)
        ctx = tools.ToolContext(conn=conn, requester=requester, today=today, docs=self.kb.docs,
                                holidays_by_doc=self.kb.holidays_by_doc, holiday_names=self.kb.holiday_names)
        try:
            result = self._dispatch(ctx, session_id, message, history, pending, deadline, request_id)
        except LLMError:
            log.warning("llm unavailable for request", extra={"request_id": request_id})
            result = clarify(R.LLM_UNAVAILABLE)
        except Exception as exc:  # never a 500 for one bad turn; nothing is stored for it
            log.error("request failed", extra={"request_id": request_id, "error": type(exc).__name__})
            result = clarify(R.LLM_UNAVAILABLE)
        result = _enforce_route_contract(result)
        result.answer = scrub_answer(result.answer)
        for call in result.tool_calls:
            call["args"] = {k: scrub_answer(v) if isinstance(v, str) else v for k, v in call["args"].items()}
        with write_transaction(conn):
            if cancelled is not None and cancelled.is_set():
                # The client already got a timeout answer: keep no proposal from this turn.
                result.proposal, result.pending_confirmation = None, False
            if result.proposal and result.pending_confirmation:
                tool_name, args = result.proposal
                repo.set_pending(conn, session_id, requester.employee_id, tool_name, args)
            repo.add_turn(conn, session_id, "user", message)
            repo.add_turn(conn, session_id, "assistant", result.answer)
        metrics.CHAT_AGENTS.labels(result.agent).inc()
        log.info("chat handled", extra={
            "request_id": request_id, "employee_id": requester.employee_id, "role": requester.role,
            "route": result.route, "agent": result.agent,
            "tools": [f"{c['name']}:{c['status']}" for c in result.tool_calls],
            "citations": [c["doc_id"] for c in result.citations], "pending": result.pending_confirmation})
        return result

    # ================================================================== routing
    def _dispatch(self, ctx, session_id, message, history, pending, deadline, request_id) -> ChatResult:
        if pending:
            if guards.is_confirmation(message):
                return self._execute_pending(ctx, pending)
            if guards.is_rejection(message):
                return clarify(R.PROPOSAL_DISCARDED)
            # Anything else: the proposal is already discarded; a changed request becomes a new proposal.
        if guards.is_manipulation(message):
            return refuse(R.MANIPULATION)
        if guards.is_bulk_request(message):
            return refuse(R.BULK)
        if guards.is_personal_data_request(message):
            return refuse(R.PERSONAL)

        plan = self._interpret(ctx, message, history, pending, deadline, request_id)
        cat = plan.category
        if cat == "manipulation":
            return refuse(R.MANIPULATION)
        if cat == "bulk_request":
            return refuse(R.BULK)
        if cat == "personal_data_request":
            return refuse(R.PERSONAL)
        if cat == "off_topic":
            return refuse(R.OFF_TOPIC)
        if cat == "greeting":
            return clarify(R.GREETING)
        if cat == "ticket":
            return self.tickets.handle(ctx, plan, message)
        if cat == "leave_action":
            proposal = self.leave.propose(ctx, plan, message, history)
            if proposal is not None:
                return proposal
        if cat == "leave_data" and not plan.reads and plan.needs_clarification and plan.clarification_question:
            # Fixed wording: the interpreter's own text could echo names or IDs it inferred.
            return clarify(R.CLARIFY_EMPLOYEE_OR_PERIOD, "leave")
        return self._answer(ctx, plan, message, deadline, request_id)

    def _answer(self, ctx, plan: Plan, message, deadline, request_id) -> ChatResult:
        """Leave records, documents, or both (the leave agent's results feed the policy agent)."""
        results: list[tools.ToolResult] = []
        if plan.reads:
            results, problem = self.leave.read(ctx, plan)
            if problem is not None:
                return problem
        wants_docs = plan.needs_documents or plan.category == "hr_question" or not plan.reads
        if not wants_docs:
            return self.leave.answer(results)
        return self.policy.answer(ctx, plan, message, results, deadline, request_id)

    def _execute_pending(self, ctx, pending: dict) -> ChatResult:
        name, args = pending["tool_name"], pending["args"]
        agent = self._write_owner.get(name)
        if agent is None:
            return clarify(R.PROPOSAL_DISCARDED)
        return agent.execute(ctx, name, args)

    # ================================================================== interpreter
    def _interpret(self, ctx, message, history, pending, deadline, request_id) -> Plan:
        r = ctx.requester
        lines = [
            "REQUEST CONTEXT:",
            f"requester: employee_id={r.employee_id}, role={r.role}, region={r.region}",
            guards.date_context(ctx.today),
            "PENDING PROPOSAL: " + (f"{pending['tool_name']} {pending['args']}" if pending else "none"),
            "RECENT CONVERSATION:",
        ]
        visible = people.visible_ids(ctx)
        lines += [f"{t['role']}: {NAMES.replace_names(t['content'], visible)[:800]}" for t in history] or ["(none)"]
        lines += ["LATEST MESSAGE:", message]
        if self.llm is None:
            raise LLMError("no LLM configured")
        data = self.llm.complete_json(purpose="interpret", system=prompts.ROUTER_SYSTEM, user="\n".join(lines),
                                      schema=prompts.ROUTER_SCHEMA, router=True, deadline=deadline,
                                      request_id=request_id, max_tokens=6000)
        plan = Plan.from_json(data)
        if plan.category == "hr_question":
            plan.reads = []  # a pure policy question needs no leave records, whatever else the model added
        log.info("plan", extra={"request_id": request_id, "category": plan.category,
                                "reads": [x.tool for x in plan.reads], "action": plan.action.get("type"),
                                "ticket": plan.ticket.get("type"), "needs_documents": plan.needs_documents})
        return plan


# ====================================================================== helpers
def _enforce_route_contract(result: ChatResult) -> ChatResult:
    """Section 8.1 invariants, applied to every response."""
    r = result
    if r.route == "rag" and not r.citations:
        return ChatResult(answer=R.NO_ANSWER, route="no_answer", agent=r.agent)
    if r.route == "hybrid":
        if not r.citations:
            r.route = "tool"
        elif not r.tool_calls:
            r.route = "rag"
    if r.route == "rag":
        r.tool_calls = []
    if r.route == "tool":
        r.citations = []
        if not r.tool_calls:
            return clarify(r.answer, r.agent)
    if r.route in ("clarify", "no_answer", "refuse"):
        r.citations, r.tool_calls = [], []
    if r.route != "tool":
        r.pending_confirmation = False
    return r
