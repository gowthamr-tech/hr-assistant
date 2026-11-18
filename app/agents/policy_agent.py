"""Policy agent: answers from the HR documents (retrieval, precedence, cited composition).

It can be given leave tool results gathered by the leave agent, for hybrid questions such as
"what will my balance be in March?". Those reach the composer as IDs and numbers only.
"""
from __future__ import annotations

from datetime import date

from app.agents import people
from app.agents.base import ChatResult, Plan
from app.orchestration import responses as R
from app.orchestration.rag import RagEngine
from app.security.pii import NAMES
from app.tools import leave_tools as tools

NAME = "policy"


class PolicyAgent:
    name = NAME

    def __init__(self, rag: RagEngine) -> None:
        self.rag = rag

    def answer(self, ctx, plan: Plan, message: str, results: list[tools.ToolResult], deadline: float,
               request_id: str) -> ChatResult:
        today = ctx.today
        tool_calls = [res.as_call() for res in results]
        agent = "leave+policy" if results else NAME
        reference = _reference_date(plan.reference_date, today)
        question = plan.search_query or message
        if plan.search_query and plan.search_query.strip().lower() != message.strip().lower():
            question = f"{plan.search_query} (employee's words: {NAMES.replace_names(message, people.visible_ids(ctx))})"
        facts = self.rag.gather_facts(question=question, queries=[plan.search_query, message],
                                      requester=ctx.requester, reference_date=reference, today=today,
                                      deadline=deadline, request_id=request_id)
        if facts.empty:
            if results:
                text = "\n\n".join(R.read_answer(res) for res in results)
                return ChatResult(answer=f"{text}\n\n{R.POLICY_PART_UNCOVERED}", route="tool",
                                  tool_calls=tool_calls, agent=agent)
            return ChatResult(answer=R.NO_ANSWER, route="no_answer", agent=NAME)
        tool_data = [_llm_safe_tool_data(res) for res in results] or None
        answer = self.rag.compose(question=question, facts=facts, requester=ctx.requester, today=today,
                                  reference_date=reference, tool_data=tool_data, deadline=deadline,
                                  request_id=request_id)
        return ChatResult(answer=answer.text, route="hybrid" if results else "rag", citations=answer.citations,
                          tool_calls=tool_calls, agent=agent)


def _reference_date(raw: str, today: date) -> date:
    d = tools._parse_date(raw)
    return d if d and d < today else today


def _llm_safe_tool_data(res: tools.ToolResult) -> dict:
    """Tool output for the composer: IDs and numbers only, no names or emails."""
    data = {k: v for k, v in res.data.items() if k != "employee"}
    if "employee" in res.data:
        data["employee_id"] = res.data["employee"].get("employee_id")
    if "leave" in data:
        data["leave"] = [{k: v for k, v in row.items() if k not in ("full_name", "email")} for row in data["leave"]]
    return {"tool": res.name, "status": res.status, "args": res.args, "result": data, "error": res.error}
