"""Specialist agents coordinated by the supervisor (``app.orchestration.orchestrator``).

- ``PolicyAgent``  answers from the HR documents (RAG, precedence, citations)
- ``LeaveAgent``   leave balances, requests, team leave; apply / cancel proposals
- ``TicketAgent``  HR support tickets: raise, follow up, comment, close / reopen; the HR queue
"""
from app.agents.leave_agent import LeaveAgent
from app.agents.policy_agent import PolicyAgent
from app.agents.ticket_agent import TicketAgent

__all__ = ["LeaveAgent", "PolicyAgent", "TicketAgent"]
