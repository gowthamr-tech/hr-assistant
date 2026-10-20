"""A scripted, recording stand-in for the LLM provider.

Tests use it to (a) make behaviour deterministic without an API key and
(b) spy on exactly what would have been sent to a real model.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.llm.client import LLMClient


def plan(category="hr_question", *, search_query="", needs_documents=None, reads=None, action=None,
         reference_date="", date_problem="", needs_clarification=False, clarification_question="") -> dict:
    if needs_documents is None:
        needs_documents = category == "hr_question"
    full_reads = []
    for r in reads or []:
        full_reads.append({"tool": r["tool"], "target": r.get("target", ""), "leave_type": r.get("leave_type", ""),
                           "status": r.get("status", ""), "start_date": r.get("start_date", ""),
                           "end_date": r.get("end_date", "")})
    act = {"type": "none", "target": "self", "leave_type": "", "start_date": "", "end_date": "", "dates_explicit_year": False,
           "request_id": "", "match_start_date": "", "match_end_date": ""}
    act.update(action or {})
    return {"category": category, "needs_documents": needs_documents, "search_query": search_query,
            "reference_date": reference_date, "reads": full_reads, "action": act, "date_problem": date_problem,
            "needs_clarification": needs_clarification, "clarification_question": clarification_question}


# Claims the fake extractor recognises in source text: (rule, regex for the value).
# Like the real extractor, a rule is only reported when the question is about it.
_PATTERNS = [
    ("volunteer_days_per_year", "volunteer", re.compile(r"(\d+) volunteer days", re.I)),
    ("sick_leave_days_per_year", "sick", re.compile(r"(\d+) (?:working )?days of paid sick leave", re.I)),
    ("expense_claim_deadline_days", "expense", re.compile(r"within (\d+) days", re.I)),
    ("wfh_days_per_week", "home", re.compile(r"(\d+) days? (?:per|a) week", re.I)),
]


def fake_extract(user: str) -> dict:
    claims = []
    question = user.split("QUESTION:", 1)[-1].split("\n", 1)[0].lower()
    docs = user.split("<documents>", 1)[-1]
    for block in re.split(r"\n(?=\[S\d+\])", docs):
        m = re.match(r"\[(S\d+)\]", block.strip())
        if not m:
            continue
        for rule, topic, rx in _PATTERNS:
            if topic not in question:
                continue
            for vm in rx.finditer(block):
                claims.append({"rule": rule, "value": vm.group(1), "statement": vm.group(0), "source": m.group(1)})
    return {"claims": claims, "unanswered": ""}


def fake_compose(user: str) -> dict:
    facts = []
    docs = user.split("<documents>", 1)[-1].split("</documents>", 1)[0]
    for line in docs.strip().splitlines():
        line = line.strip()
        if line.startswith("{") and '"unanswered"' not in line[:16]:
            facts.append(json.loads(line))
    parts = []
    for f in facts:
        vals = " / ".join(s["value"] for s in f["sources"])
        parts.append(f"{f['rule']}={vals} [{f['status']}]")
    return {"answer": "; ".join(parts) or "nothing", "used_facts": [f["id"] for f in facts]}


@dataclass
class Call:
    purpose: str
    system: str
    user: str
    model: str


@dataclass
class FakeProvider:
    router: Callable[[str], dict]
    extractor: Callable[[str], dict] = fake_extract
    composer: Callable[[str], dict] = fake_compose
    calls: list[Call] = field(default_factory=list)
    fail: bool = False
    name: str = "fake"

    def complete(self, *, system, user, schema, model, max_tokens, timeout) -> str:
        if system.startswith("You are the request interpreter"):
            purpose, fn = "interpret", self.router
        elif system.startswith("You extract facts"):
            purpose, fn = "extract_claims", self.extractor
        else:
            purpose, fn = "compose_answer", self.composer
        self.calls.append(Call(purpose, system, user, model))
        if self.fail:
            raise RuntimeError("simulated provider outage")
        return json.dumps(fn(user if purpose != "interpret" else _latest_message(user)))

    def all_text(self) -> str:
        return "\n".join(c.system + "\n" + c.user for c in self.calls)


def _latest_message(user: str) -> str:
    return user.rsplit("LATEST MESSAGE:\n", 1)[-1]


def make_client(router: Callable[[str], dict], **kw) -> tuple[LLMClient, FakeProvider]:
    provider = FakeProvider(router=router, **kw)
    return LLMClient(provider, "fake-model", "fake-router", 3000, 10.0), provider


class ScriptedRouter:
    """Maps message substrings to plans; the first matching key wins."""

    def __init__(self, rules: Optional[dict[str, dict]] = None, default: Optional[dict] = None) -> None:
        self.rules = rules or {}
        self.default = default or plan("off_topic")

    def __call__(self, message: str) -> dict:
        low = message.lower()
        for key, value in self.rules.items():
            if key.lower() in low:
                return value
        return self.default
