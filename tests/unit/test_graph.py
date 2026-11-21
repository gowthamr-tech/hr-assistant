"""The supervisor's LangGraph: node set and the pure routing functions on its conditional edges."""
import pytest

from app.agents.base import ChatResult, Plan, Read
from app.agents.leave_agent import LeaveAgent
from app.agents.ticket_agent import TicketAgent
from app.orchestration import orchestrator as orch


def _plan(category, reads=(), **kw):
    return Plan(category=category, reads=[Read(tool=t) for t in reads], **kw)


def test_graph_has_every_agent_node_and_ends_in_route_contract():
    o = orch.Orchestrator.__new__(orch.Orchestrator)
    o.leave, o.tickets, o.policy, o._write_owner = LeaveAgent(), TicketAgent(), None, {}
    g = o.build_graph().get_graph()
    assert {"execute_pending", "discard_pending", "guards", "interpret", "fixed_reply", "ticket_agent",
            "leave_propose", "leave_read", "leave_answer", "policy_agent", "route_contract"} <= set(g.nodes)
    into_end = {e.source for e in g.edges if e.target == "__end__"}
    assert into_end == {"route_contract"}


@pytest.mark.parametrize("message,pending,expected", [
    ("yes", {"tool_name": "apply_leave", "args": {}}, "execute_pending"),
    ("no", {"tool_name": "apply_leave", "args": {}}, "discard_pending"),
    ("actually make it Friday", {"tool_name": "apply_leave", "args": {}}, "guards"),
    ("yes", None, "guards"),
])
def test_pending_proposal_routing(message, pending, expected):
    assert orch._route_pending({"message": message, "pending": pending}) == expected


@pytest.mark.parametrize("plan,expected", [
    (_plan("manipulation"), "fixed_reply"),
    (_plan("greeting"), "fixed_reply"),
    (_plan("off_topic"), "fixed_reply"),
    (_plan("ticket"), "ticket_agent"),
    (_plan("leave_action"), "leave_propose"),
    (_plan("leave_data", needs_clarification=True, clarification_question="Whose?"), "fixed_reply"),
    (_plan("leave_data", reads=["get_leave_balance"]), "leave_read"),
    (_plan("hr_question"), "policy_agent"),
])
def test_plan_category_routing(plan, expected):
    assert orch._route_plan({"plan": plan}) == expected


def test_after_reads_goes_to_policy_only_when_documents_are_needed():
    done = ChatResult(answer="no", route="refuse")
    reads = _plan("leave_data", reads=["get_leave_balance"])
    hybrid = _plan("leave_data", reads=["get_leave_balance"], needs_documents=True)
    assert orch._route_after_reads({"plan": reads, "result": done}) == "route_contract"
    assert orch._route_after_reads({"plan": reads, "result": None}) == "leave_answer"
    assert orch._route_after_reads({"plan": hybrid, "result": None}) == "policy_agent"


def test_leave_action_without_a_proposal_falls_through_to_an_answer():
    plan = _plan("leave_action")
    assert orch._done_or_answer({"plan": plan, "result": None}) == "policy_agent"
    assert orch._done_or_answer({"plan": plan, "result": ChatResult("x", "tool")}) == "route_contract"
