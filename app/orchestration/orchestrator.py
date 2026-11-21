"""Supervisor: one message in, one specialist agent (or two) out, exactly one route.

The turn is a LangGraph ``StateGraph`` (built once in ``build_graph``):

    START ─┬─ confirmed pending proposal ──> execute_pending ───────────────┐
           ├─ "no" to a pending proposal ──> discard_pending ───────────────┤
           └─> guards ── refused ───────────────────────────────────────────┤
                 └─> interpret (LLM) ─┬─ refuse / greeting / clarify ─> fixed_reply ──┤
                                      ├─ ticket ──────> ticket_agent ───────┤
                                      ├─ leave_action > leave_propose ──────┤
                                      │                  └ (no proposal) ┐  │
                                      ├─ reads ───────> leave_read ─┬─ problem ───────┤
                                      │                             ├─> leave_answer ─┤
                                      │                             └─> policy_agent ─┤
                                      └─ documents ─────────────────> policy_agent ───┤
                                                                      route_contract <┘ ─> END

Order of decisions for every message:
  1. A pending proposal is consumed. A clear confirmation hands it to the agent that owns the
     tool (leave or ticket) to execute; a clear "no" discards it.
  2. Deterministic guards: manipulation, bulk data, personal data -> refuse.
  3. The LLM interpreter turns the message into a plan (no corpus text in that call). Its
     category is the routing decision (a conditional edge).
  4. The plan is handed to a specialist agent node:
       hr_question               -> PolicyAgent (documents, citations)
       leave_data                -> LeaveAgent reads, plus PolicyAgent when rules are needed (hybrid)
       leave_action              -> LeaveAgent proposal
       ticket                    -> TicketAgent (support tickets)
     Agents run code: permission pre-checks, tools, retrieval, precedence, composition.
  5. Every path ends in route_contract (Section 8.1 invariants).
The LLM never chooses who the requester is, and never executes a tool by itself: graph edges are
decided by code reading the plan, not by the model calling tools.

Persistence stays outside the graph, in ``Orchestrator.handle``: the turn and any proposal are
stored in one SQLite transaction after the response is built, so no LangGraph checkpointer is used.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

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
__all__ = ["ChatResult", "Orchestrator", "Plan", "Read", "TurnState", "clarify", "refuse"]
_current_leave_thread = current_leave_thread


class TurnState(TypedDict, total=False):
    """Graph state for one turn. Never checkpointed, so it may hold the live connection context."""
    ctx: Any  # tools.ToolContext
    message: str
    history: list[dict]
    pending: Optional[dict]
    deadline: float
    request_id: str
    plan: Plan
    reads: list  # list[tools.ToolResult] from the leave agent, fed to the policy agent for hybrid answers
    result: Optional[ChatResult]


_FIXED_REPLIES = {
    "manipulation": lambda: refuse(R.MANIPULATION),
    "bulk_request": lambda: refuse(R.BULK),
    "personal_data_request": lambda: refuse(R.PERSONAL),
    "off_topic": lambda: refuse(R.OFF_TOPIC),
    "greeting": lambda: clarify(R.GREETING),
}


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
        self.graph = self.build_graph()

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
        state: TurnState = {"ctx": ctx, "message": message, "history": history, "pending": pending,
                            "deadline": deadline, "request_id": request_id, "reads": [], "result": None}
        try:
            result = self.graph.invoke(state)["result"]
        except LLMError:
            log.warning("llm unavailable for request", extra={"request_id": request_id})
            result = clarify(R.LLM_UNAVAILABLE)
        except Exception as exc:  # never a 500 for one bad turn; nothing is stored for it
            log.error("request failed", extra={"request_id": request_id, "error": type(exc).__name__})
            result = clarify(R.LLM_UNAVAILABLE)
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

    # ================================================================== graph
    def build_graph(self):
        g = StateGraph(TurnState)
        g.add_node("execute_pending", self._node_execute_pending)
        g.add_node("discard_pending", lambda s: {"result": clarify(R.PROPOSAL_DISCARDED)})
        g.add_node("guards", self._node_guards)
        g.add_node("interpret", self._node_interpret)
        g.add_node("fixed_reply", self._node_fixed_reply)
        g.add_node("ticket_agent", self._node_ticket)
        g.add_node("leave_propose", self._node_leave_propose)
        g.add_node("leave_read", self._node_leave_read)
        g.add_node("leave_answer", lambda s: {"result": self.leave.answer(s["reads"])})
        g.add_node("policy_agent", self._node_policy)
        g.add_node("route_contract", lambda s: {"result": _enforce_route_contract(s["result"])})

        g.add_conditional_edges(START, _route_pending, ["execute_pending", "discard_pending", "guards"])
        g.add_conditional_edges("guards", _done_or("interpret"), ["route_contract", "interpret"])
        g.add_conditional_edges("interpret", _route_plan,
                                ["fixed_reply", "ticket_agent", "leave_propose", "leave_read", "policy_agent"])
        g.add_conditional_edges("leave_propose", _done_or_answer, ["route_contract", "leave_read", "policy_agent"])
        g.add_conditional_edges("leave_read", _route_after_reads, ["route_contract", "leave_answer", "policy_agent"])
        for node in ("execute_pending", "discard_pending", "fixed_reply", "ticket_agent", "leave_answer",
                     "policy_agent"):
            g.add_edge(node, "route_contract")
        g.add_edge("route_contract", END)
        return g.compile()

    # ------------------------------------------------------------------ nodes
    def _node_execute_pending(self, s: TurnState) -> dict:
        name, args = s["pending"]["tool_name"], s["pending"]["args"]
        agent = self._write_owner.get(name)
        if agent is None:
            return {"result": clarify(R.PROPOSAL_DISCARDED)}
        return {"result": agent.execute(s["ctx"], name, args)}

    def _node_guards(self, s: TurnState) -> dict:
        # Anything other than yes/no to a pending proposal lands here: the proposal is already
        # discarded, and a changed request becomes a new proposal.
        message = s["message"]
        if guards.is_manipulation(message):
            return {"result": refuse(R.MANIPULATION)}
        if guards.is_bulk_request(message):
            return {"result": refuse(R.BULK)}
        if guards.is_personal_data_request(message):
            return {"result": refuse(R.PERSONAL)}
        return {}

    def _node_interpret(self, s: TurnState) -> dict:
        return {"plan": self._interpret(s["ctx"], s["message"], s["history"], s["pending"], s["deadline"],
                                        s["request_id"])}

    def _node_fixed_reply(self, s: TurnState) -> dict:
        reply = _FIXED_REPLIES.get(s["plan"].category)
        if reply is not None:
            return {"result": reply()}
        # leave_data that names nobody / no period. Fixed wording: the interpreter's own text could
        # echo names or IDs it inferred.
        return {"result": clarify(R.CLARIFY_EMPLOYEE_OR_PERIOD, "leave")}

    def _node_ticket(self, s: TurnState) -> dict:
        return {"result": self.tickets.handle(s["ctx"], s["plan"], s["message"])}

    def _node_leave_propose(self, s: TurnState) -> dict:
        # None means the plan held no apply / cancel action: the turn continues as a read or a question.
        return {"result": self.leave.propose(s["ctx"], s["plan"], s["message"], s["history"])}

    def _node_leave_read(self, s: TurnState) -> dict:
        results, problem = self.leave.read(s["ctx"], s["plan"])
        return {"reads": results, "result": problem}

    def _node_policy(self, s: TurnState) -> dict:
        """Documents, optionally combined with the leave agent's results (a hybrid answer)."""
        return {"result": self.policy.answer(s["ctx"], s["plan"], s["message"], s.get("reads") or [],
                                             s["deadline"], s["request_id"])}

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


# ====================================================================== edges (pure routing, no side effects)
def _route_pending(s: TurnState) -> str:
    if s.get("pending"):
        if guards.is_confirmation(s["message"]):
            return "execute_pending"
        if guards.is_rejection(s["message"]):
            return "discard_pending"
    return "guards"


def _done_or(next_node: str):
    def route(s: TurnState) -> str:
        return "route_contract" if s.get("result") is not None else next_node
    return route


def _answer_path(plan: Plan) -> str:
    """Leave records first when the plan has reads (they may feed a hybrid answer), else documents."""
    return "leave_read" if plan.reads else "policy_agent"


def _route_plan(s: TurnState) -> str:
    plan = s["plan"]
    cat = plan.category
    if cat in _FIXED_REPLIES:
        return "fixed_reply"
    if cat == "ticket":
        return "ticket_agent"
    if cat == "leave_action":
        return "leave_propose"
    if cat == "leave_data" and not plan.reads and plan.needs_clarification and plan.clarification_question:
        return "fixed_reply"
    return _answer_path(plan)


def _done_or_answer(s: TurnState) -> str:
    return "route_contract" if s.get("result") is not None else _answer_path(s["plan"])


def _route_after_reads(s: TurnState) -> str:
    if s.get("result") is not None:  # a refusal or clarification from the leave agent's pre-checks
        return "route_contract"
    plan = s["plan"]
    wants_docs = plan.needs_documents or plan.category == "hr_question" or not plan.reads
    return "policy_agent" if wants_docs else "leave_answer"


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
