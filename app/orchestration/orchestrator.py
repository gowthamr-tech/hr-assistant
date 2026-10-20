"""Conversation orchestration: one message in, exactly one route out.

Order of decisions for every message:
  1. A pending proposal is consumed. A clear confirmation executes it; a clear "no" discards it.
  2. Deterministic guards: manipulation, bulk data, personal data -> refuse.
  3. The LLM interpreter turns the message into a plan (no corpus text in that call).
  4. Code executes the plan: permission pre-checks, tools, retrieval, precedence, composition.
The LLM never chooses who the requester is, and never executes a tool by itself.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

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
from app.security.access import EMPLOYEE_REFUSAL, can_use_team_leave, can_view_leave_of
from app.security.pii import NAMES, scrub_answer
from app.tools import leave_tools as tools

log = logging.getLogger(__name__)

_EMPLOYEE_ID = re.compile(r"^[A-Za-z]\d{3,6}$")
_SELF_WORDS = {"", "self", "me", "my", "mine", "i", "myself", "requester", "you"}


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


@dataclass
class Read:
    tool: str
    target: str = ""
    leave_type: str = ""
    status: str = ""
    start_date: str = ""
    end_date: str = ""


@dataclass
class Plan:
    category: str = "hr_question"
    needs_documents: bool = False
    search_query: str = ""
    reference_date: str = ""
    reads: list[Read] = field(default_factory=list)
    action: dict = field(default_factory=dict)
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
        return cls(
            category=str(data.get("category") or "hr_question"),
            needs_documents=bool(data.get("needs_documents")),
            search_query=str(data.get("search_query") or ""),
            reference_date=str(data.get("reference_date") or ""),
            reads=reads[:4],
            action=action,
            date_problem=str(data.get("date_problem") or ""),
            needs_clarification=bool(data.get("needs_clarification")),
            clarification_question=str(data.get("clarification_question") or ""),
        )


def refuse(msg: str) -> ChatResult:
    log.info("refused", extra={"reason": msg})
    return ChatResult(answer=msg, route="refuse")


def clarify(msg: str) -> ChatResult:
    return ChatResult(answer=msg, route="clarify")


class Orchestrator:
    def __init__(self, kb: KnowledgeBase, llm: Optional[LLMClient], settings: Settings) -> None:
        self.kb = kb
        self.llm = llm
        self.settings = settings
        self.rag = RagEngine(kb, llm, settings.max_corpus_tokens)

    # ================================================================== entry point
    def handle(self, conn: sqlite3.Connection, requester: Employee, session_id: str, message: str,
               request_id: str, cancelled: Optional[threading.Event] = None) -> ChatResult:
        deadline = time.monotonic() + self.settings.request_deadline_s
        today = self.settings.today()
        history = repo.recent_turns(conn, session_id, limit=6)
        pending = repo.take_pending(conn, session_id, requester.employee_id)
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
        log.info("chat handled", extra={
            "request_id": request_id, "employee_id": requester.employee_id, "role": requester.role,
            "route": result.route, "tools": [f"{c['name']}:{c['status']}" for c in result.tool_calls],
            "citations": [c["doc_id"] for c in result.citations], "pending": result.pending_confirmation})
        return result

    # ================================================================== dispatch
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
        if cat == "leave_action":
            kind = (plan.action or {}).get("type")
            if kind == "apply_leave":
                
                return self._propose_apply(ctx, session_id, plan, message, history)
            if kind == "cancel_leave":
                return self._propose_cancel(ctx, session_id, plan, message)
        if cat == "leave_data" and not plan.reads and plan.needs_clarification and plan.clarification_question:
            # Fixed wording: the interpreter's own text could echo names or IDs it inferred.
            return clarify(R.CLARIFY_EMPLOYEE_OR_PERIOD)
        return self._answer(ctx, plan, message, deadline, request_id)

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
        visible = self._visible_ids(ctx)
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
                                "needs_documents": plan.needs_documents})
        return plan

    # ================================================================== read + documents
    def _answer(self, ctx, plan: Plan, message, deadline, request_id) -> ChatResult:
        today = ctx.today
        results: list[tools.ToolResult] = []
        if plan.reads:
            prepared, problem = self._prepare_reads(ctx, plan)
            if problem is not None:
                return problem
            for read, target_id in prepared:
                results.append(self._run_read(ctx, read, target_id))
        tool_calls = [res.as_call() for res in results]

        wants_docs = plan.needs_documents or plan.category == "hr_question" or not plan.reads
        if not wants_docs:
            return ChatResult(answer="\n\n".join(R.read_answer(res) for res in results), route="tool",
                              tool_calls=tool_calls)

        reference = _reference_date(plan.reference_date, today)
        question = plan.search_query or message
        if plan.search_query and plan.search_query.strip().lower() != message.strip().lower():
            question = f"{plan.search_query} (employee's words: {NAMES.replace_names(message, self._visible_ids(ctx))})"
        facts = self.rag.gather_facts(question=question, queries=[plan.search_query, message],
                                      requester=ctx.requester, reference_date=reference, today=today,
                                      deadline=deadline, request_id=request_id)
        if facts.empty:
            if results:
                text = "\n\n".join(R.read_answer(res) for res in results)
                return ChatResult(answer=f"{text}\n\nThe HR documents don't cover the policy part of your question; "
                                         "please contact the HR team for that.", route="tool", tool_calls=tool_calls)
            return ChatResult(answer=R.NO_ANSWER, route="no_answer")
        tool_data = [_llm_safe_tool_data(res) for res in results] or None
        answer = self.rag.compose(question=question, facts=facts, requester=ctx.requester, today=today,
                                  reference_date=reference, tool_data=tool_data, deadline=deadline,
                                  request_id=request_id)
        return ChatResult(answer=answer.text, route="hybrid" if results else "rag", citations=answer.citations,
                          tool_calls=tool_calls)

    def _prepare_reads(self, ctx, plan: Plan):
        prepared = []
        others: set[str] = set()
        for read in plan.reads:
            if read.tool == "get_team_leave":
                if not can_use_team_leave(ctx.requester):
                    return None, refuse(R.TEAM_ONLY_MANAGERS)
                if not (_iso(read.start_date) and _iso(read.end_date)):
                    return None, clarify("Which dates should I check for your team's leave?")
                prepared.append((read, None))
                continue
            status, value = self._resolve_target(ctx, read.target)
            if status == "refuse":
                return None, refuse(EMPLOYEE_REFUSAL)
            if status == "clarify":
                return None, clarify(value)
            if value != ctx.requester.employee_id:
                others.add(value)
            prepared.append((read, value))
        if len(others) > 1:
            return None, refuse(R.BULK)
        for read, target_id in prepared:
            # Pre-check with the same rule the tool enforces, so a refusal carries no tool call.
            if target_id and not can_view_leave_of(ctx.requester, repo.get_employee(ctx.conn, target_id)):
                return None, refuse(EMPLOYEE_REFUSAL)
        return prepared, None

    def _visible_ids(self, ctx) -> set[str]:
        me = ctx.requester
        if me.role == "hr":
            return {e.employee_id for e in repo.list_all_employees(ctx.conn)}
        ids = {me.employee_id}
        if me.role == "manager":
            ids |= {e.employee_id for e in repo.get_direct_reports(ctx.conn, me.employee_id)}
        return ids

    def _names_someone_else(self, ctx, message: str, target: str) -> bool:
        """Write tools act for the requester only. Refuse when the request names another person."""
        t = (target or "").strip()
        if t and t.lower() not in _SELF_WORDS:
            status, value = self._resolve_target(ctx, t)
            if status != "ok" or value != ctx.requester.employee_id:
                return True
        for emp_id in re.findall(r"(?<![\w-])[Ee]\d{3,6}(?![\w-])", message):
            if emp_id.upper() != ctx.requester.employee_id:
                return True
        return NAMES.mentions_other_than(message, ctx.requester.employee_id)

    def _resolve_target(self, ctx, target: str) -> tuple[str, str]:
        t = (target or "").strip()
        me = ctx.requester
        if t.lower() in _SELF_WORDS:
            return "ok", me.employee_id
        if _EMPLOYEE_ID.match(t):
            return "ok", t.upper()
        visible = [me]
        if me.role == "manager":
            visible += repo.get_direct_reports(ctx.conn, me.employee_id)
        elif me.role == "hr":
            visible = repo.list_all_employees(ctx.conn)
        words = [w for w in re.split(r"\s+", t.lower()) if w]
        matches = [e for e in visible if all(w in e.full_name.lower().split() for w in words)]
        if len(matches) == 1:
            return "ok", matches[0].employee_id
        if not matches:
            return "refuse", ""
        options = ", ".join(f"{e.full_name} ({e.employee_id})" for e in matches[:6])
        return "clarify", f"Which employee do you mean: {options}?"

    def _run_read(self, ctx, read: Read, target_id: Optional[str]) -> tools.ToolResult:
        if read.tool == "get_leave_balance":
            return tools.get_leave_balance(ctx, target_id, read.leave_type or None)
        if read.tool == "list_leave_requests":
            return tools.list_leave_requests(ctx, target_id, read.status or None)
        return tools.get_team_leave(ctx, read.start_date, read.end_date)

    # ================================================================== apply proposal
    def _propose_apply(self, ctx, session_id, plan: Plan, message, history) -> ChatResult:
        a = plan.action
        if self._names_someone_else(ctx, message, a.get("target", "")):
            return refuse(R.SELF_ONLY)
        user_texts = [message] + _current_leave_thread(history)
        leave_type = a.get("leave_type") or ""
        if leave_type and not guards.leave_type_stated(user_texts, leave_type):
            leave_type = ""  # the interpreter guessed; the rules say ask
        start_s, end_s = str(a.get("start_date") or ""), str(a.get("end_date") or a.get("start_date") or "")
        missing = []
        if not leave_type:
            missing.append("whether this is annual or sick leave")
        if plan.date_problem in ("missing", "ambiguous") or not (_iso(start_s) and _iso(end_s)):
            missing.append("the exact start and end dates" if plan.date_problem != "ambiguous"
                           else "the exact start and end dates (the dates you gave could mean more than one day)")
        if missing:
            return clarify("To set up the request I need " + " and ".join(missing) + ".")
        start = date.fromisoformat(start_s)
        if start < ctx.today and not a.get("dates_explicit_year"):
            return clarify(f"{R.fmt_date(start_s)} has already passed (today is {R.fmt_date(ctx.today.isoformat())}). "
                           "Which dates did you mean?")
        check = tools.apply_leave(ctx, leave_type, start_s, end_s, dry_run=True)
        if check.status == "error":
            return ChatResult(answer=R.rejection(check), route="tool", tool_calls=[check.as_call()])
        return ChatResult(answer=R.apply_proposal(check), route="tool",
                          tool_calls=[check.as_call("pending_confirmation")], pending_confirmation=True,
                          proposal=("apply_leave", check.args))

    # ================================================================== cancel proposal
    def _propose_cancel(self, ctx, session_id, plan: Plan, message: str) -> ChatResult:
        a = plan.action
        if self._names_someone_else(ctx, message, a.get("target", "")):
            return refuse(R.SELF_ONLY)
        request_id = str(a.get("request_id") or "").strip()
        if not request_id:
            mine = [r for r in repo.list_requests(ctx.conn, ctx.requester.employee_id)
                    if r.status in ("pending", "approved")]
            ms, me = _iso(a.get("match_start_date")), _iso(a.get("match_end_date") or a.get("match_start_date"))
            candidates = mine
            if ms and me:
                candidates = [r for r in candidates if r.start_date <= me.isoformat() and r.end_date >= ms.isoformat()]
            if a.get("leave_type"):
                candidates = [r for r in candidates if r.leave_type == a["leave_type"]]
            future = [r for r in candidates if date.fromisoformat(r.start_date) > ctx.today]
            pick = future if future else candidates
            if len(pick) == 1:
                request_id = pick[0].request_id
            elif not pick:
                if not mine:
                    return clarify("You don't have any pending or approved leave requests to cancel.")
                return clarify("I couldn't find a matching request. Your pending and approved requests are:\n"
                               + R.request_choices(mine) + "\nWhich one would you like to cancel?")
            else:
                return clarify("You have more than one matching request:\n" + R.request_choices(pick)
                               + "\nWhich one would you like to cancel?")
        check = tools.cancel_leave(ctx, request_id, dry_run=True)
        if check.status == "error":
            return ChatResult(answer=R.rejection(check), route="tool", tool_calls=[check.as_call()])
        return ChatResult(answer=R.cancel_proposal(check), route="tool",
                          tool_calls=[check.as_call("pending_confirmation")], pending_confirmation=True,
                          proposal=("cancel_leave", check.args))

    # ================================================================== execute
    def _execute_pending(self, ctx, pending: dict) -> ChatResult:
        name, args = pending["tool_name"], pending["args"]
        try:
            if name == "apply_leave":
                res = tools.apply_leave(ctx, args.get("leave_type"), args.get("start_date"), args.get("end_date"))
                text = R.apply_done(res)
            elif name == "cancel_leave":
                res = tools.cancel_leave(ctx, args.get("request_id"))
                text = R.cancel_done(res)
            else:
                return clarify(R.PROPOSAL_DISCARDED)
        except sqlite3.Error:
            log.exception("write action failed", extra={"tool": name})
            res = tools.ToolResult(name=name, args=args, status="error", error="database error")
            text = "Something went wrong while saving, and nothing was changed. Please try again."
        return ChatResult(answer=text, route="tool", tool_calls=[res.as_call()])


# ====================================================================== helpers
_LEAVE_FLOW_PREFIXES = ("To set up the request", "Please confirm:")


def _current_leave_thread(history: list[dict]) -> list[str]:
    """User messages of the leave request in progress: walk back while each assistant reply was
    this flow's own question or proposal. An unrelated earlier question ("what is the sick leave
    policy?") is not the user choosing a leave type."""
    texts: list[str] = []
    i = len(history) - 1
    while i >= 1 and history[i]["role"] == "assistant" and history[i - 1]["role"] == "user":
        reply = history[i]["content"]
        if not (reply.startswith(_LEAVE_FLOW_PREFIXES) or "has already passed" in reply):
            break
        texts.append(history[i - 1]["content"])
        i -= 2
    return texts



def _iso(value) -> Optional[date]:
    return tools._parse_date(value)


def _reference_date(raw: str, today: date) -> date:
    d = _iso(raw)
    return d if d and d < today else today


def _llm_safe_tool_data(res: tools.ToolResult) -> dict:
    """Tool output for the composer: IDs and numbers only, no names or emails."""
    data = {k: v for k, v in res.data.items() if k != "employee"}
    if "employee" in res.data:
        data["employee_id"] = res.data["employee"].get("employee_id")
    if "leave" in data:
        data["leave"] = [{k: v for k, v in row.items() if k not in ("full_name", "email")} for row in data["leave"]]
    return {"tool": res.name, "status": res.status, "args": res.args, "result": data, "error": res.error}


def _enforce_route_contract(result: ChatResult) -> ChatResult:
    """Section 8.1 invariants, applied to every response."""
    r = result
    if r.route == "rag" and not r.citations:
        return ChatResult(answer=R.NO_ANSWER, route="no_answer")
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
            return clarify(r.answer)
    if r.route in ("clarify", "no_answer", "refuse"):
        r.citations, r.tool_calls = [], []
    if r.route != "tool":
        r.pending_confirmation = False
    return r
