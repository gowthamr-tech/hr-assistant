"""Document answers: retrieve -> extract claims (LLM) -> precedence (code) -> compose (LLM).

Restricted documents are excluded before retrieval scoring, so their text can
never reach either LLM call. Each call's corpus text is kept under the budget
here, and the LLM client re-checks the count before sending.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from app.db.repositories import Employee
from app.ingestion.models import Chunk, DocMeta
from app.ingestion.pipeline import KnowledgeBase
from app.llm import prompts
from app.llm.client import LLMClient, LLMError, corpus_token_count
from app.policy.eligibility import eligible_docs, upcoming_docs
from app.policy.precedence import Claim, find_upcoming, resolve_all

log = logging.getLogger(__name__)

REGION_NAMES = {"IN": "India", "US": "United States", "DE": "Germany"}
MAX_CHUNKS = 24  # the 3,000-token budget is the real limit; this only caps noise
MAX_CHUNKS_PER_DOC = 3


@dataclass
class Source:
    label: str
    chunk: Chunk
    doc: DocMeta

    def render(self) -> str:
        return f"[{self.label}] {self.doc.title} | {self.chunk.locator}\n{self.chunk.text}"


@dataclass
class Fact:
    fact_id: str
    rule: str
    status: str  # "current" | "conflict" | "upcoming"
    claims: list[Claim]

    def to_prompt(self) -> dict:
        entries = [{"value": c.value, "statement": c.statement, "document": c.doc.title,
                    "effective_date": c.doc.effective_date.isoformat()} for c in self.claims]
        return {"id": self.fact_id, "rule": self.rule, "status": self.status, "sources": entries}

    def citations(self) -> list[tuple[str, str]]:
        return [(c.doc.doc_id, c.locator) for c in self.claims]


@dataclass
class FactSet:
    facts: list[Fact] = field(default_factory=list)
    unanswered: str = ""
    sources_used: int = 0
    corpus_tokens: int = 0

    @property
    def empty(self) -> bool:
        return not self.facts


@dataclass
class Answer:
    text: str
    citations: list[dict]


class RagEngine:
    def __init__(self, kb: KnowledgeBase, llm: Optional[LLMClient], max_corpus_tokens: int) -> None:
        self.kb = kb
        self.llm = llm
        self.max_corpus_tokens = max_corpus_tokens

    # ------------------------------------------------------------------ retrieval
    def  select_sources(self, requester: Employee, queries: list[str], reference_date: date,
                       include_future: bool) -> list[Source]:
        docs = list(self.kb.docs.values())
        allowed = {d.doc_id for d in eligible_docs(docs, requester, reference_date)}
        if include_future:
            allowed |= {d.doc_id for d in upcoming_docs(docs, requester, reference_date)}
        ranked = self.kb.index.search(queries, allowed)

        # Diversity first: the best chunk of each document, then further chunks by score.
        # Precedence only works if competing documents are all in front of the extractor.
        firsts, rest, seen = [], [], set()
        for sc in ranked:
            (rest if sc.chunk.doc_id in seen else firsts).append(sc)
            seen.add(sc.chunk.doc_id)
        ordered = firsts + rest

        chosen: list[Source] = []
        per_doc: dict[str, int] = {}
        for sc in ordered:
            if len(chosen) >= MAX_CHUNKS:
                break
            doc_id = sc.chunk.doc_id
            if doc_id not in allowed or per_doc.get(doc_id, 0) >= MAX_CHUNKS_PER_DOC:
                continue
            candidate = Source(label="", chunk=sc.chunk, doc=self.kb.docs[doc_id])
            trial = chosen + [candidate]
            self._relabel(trial)
            if corpus_token_count([s.render() for s in trial]) > self.max_corpus_tokens:
                continue  # skip this one, a smaller chunk further down may still fit
            chosen = trial
            per_doc[doc_id] = per_doc.get(doc_id, 0) + 1
        # Restore document order so related passages read naturally; labels follow.
        chosen.sort(key=lambda s: (s.doc.doc_id, s.chunk.chunk_id))
        self._relabel(chosen)
        return chosen

    @staticmethod
    def _relabel(sources: list[Source]) -> None:
        for i, s in enumerate(sources, start=1):
            s.label = f"S{i}"

    # ------------------------------------------------------------------ facts
    def gather_facts(self, *, question: str, queries: list[str], requester: Employee, reference_date: date,
                     today: date, deadline: Optional[float], request_id: str) -> FactSet:
        historical = reference_date < today
        sources = self.select_sources(requester, queries, reference_date, include_future=not historical)
        if not sources:
            return FactSet()
        blocks = [s.render() for s in sources]
        if self.llm is None:
            raise LLMError("no LLM configured")
        user = (
            f"EMPLOYEE REGION: {requester.region} ({REGION_NAMES.get(requester.region, requester.region)})\n"
            f"EMPLOYEE LEVEL: {requester.level}\n"
            f"QUESTION: {question}\n"
            "Excerpts are labelled [S1], [S2], ... below."
        )
        out = self.llm.complete_json(purpose="extract_claims", system=prompts.EXTRACT_SYSTEM, user=user,
                                     schema=prompts.EXTRACT_SCHEMA, corpus_blocks=blocks, deadline=deadline,
                                     request_id=request_id, max_tokens=8000)
        by_label = {s.label: s for s in sources}
        current, future = [], []
        for raw in out.get("claims") or []:
            src = by_label.get(str(raw.get("source", "")).strip().strip("[]"))
            rule = str(raw.get("rule", "")).strip().lower()
            value = str(raw.get("value", "")).strip()
            if src is None or not rule or not value:
                continue  # unknown label: never attribute a claim to a document we did not send
            claim = Claim(rule=rule, value=value, statement=str(raw.get("statement", "")).strip(),
                          doc=src.doc, locator=src.chunk.locator, source_label=src.label)
            (future if src.doc.effective_date > reference_date else current).append(claim)

        facts: list[Fact] = []
        for res in resolve_all(current):
            facts.append(Fact(fact_id="", rule=res.rule, status="conflict" if res.conflict else "current",
                              claims=_dedupe(res.winners)))
            log.info("precedence resolved", extra={
                "request_id": request_id, "rule": res.rule, "winners": sorted({c.doc.doc_id for c in res.winners}),
                "losers": sorted({c.doc.doc_id for c in res.losers}), "decided_by": res.decided_by,
                "conflict": res.conflict})
        if not historical:
            for up in find_upcoming(current, future):
                facts.append(Fact(fact_id="", rule=up.rule, status="upcoming", claims=[up.claim]))
        for i, f in enumerate(facts, start=1):
            f.fact_id = f"F{i}"
        return FactSet(facts=facts, unanswered=str(out.get("unanswered") or "").strip(),
                       sources_used=len(sources), corpus_tokens=corpus_token_count(blocks))

    # ------------------------------------------------------------------ answer
    def compose(self, *, question: str, facts: FactSet, requester: Employee, today: date,
                reference_date: date, tool_data: Optional[list[dict]], deadline: Optional[float],
                request_id: str) -> Answer:
        fact_blocks = self._fact_blocks(facts)
        context = {
            "today": today.isoformat(),
            "reference_date": reference_date.isoformat(),
            "employee": {"employee_id": requester.employee_id, "region": requester.region,
                         "level": requester.level, "join_date": requester.join_date,
                         "years_of_service_today": _years_between(date.fromisoformat(requester.join_date), today)},
        }
        user = f"QUESTION: {question}\nCONTEXT: {json.dumps(context)}\n"
        if facts.unanswered:
            # extractor output is corpus-derived: it travels as a counted corpus block, capped
            fact_blocks = fact_blocks + [json.dumps({"unanswered": facts.unanswered[:300]})]
            if corpus_token_count(fact_blocks) > self.max_corpus_tokens:
                fact_blocks = fact_blocks[:-1]
        if tool_data:
            user += f"TOOL RESULTS (structured database records): {json.dumps(tool_data, default=str)}\n"
        user += ("FACTS are in <documents> below, one JSON object per fact; a final {\"unanswered\": ...} "
                 "object, if present, lists what the documents do not cover.")
        try:
            if self.llm is None:
                raise LLMError("no LLM configured")
            out = self.llm.complete_json(purpose="compose_answer", system=prompts.COMPOSE_SYSTEM, user=user,
                                         schema=prompts.COMPOSE_SCHEMA, corpus_blocks=fact_blocks,
                                         deadline=deadline, request_id=request_id, max_tokens=8000)
            text = str(out.get("answer") or "").strip()
            used = {str(x).strip() for x in out.get("used_facts") or []}
            if not text:
                raise LLMError("empty answer")
        except LLMError as exc:
            log.warning("compose failed; using template answer", extra={
                "request_id": request_id, "error": str(exc)[:200],
                "cause": type(exc.__cause__).__name__ if exc.__cause__ else ""})
            text, used = self._template_answer(facts), {f.fact_id for f in facts.facts}
        # Cite exactly the facts the answer used; a conflict fact cites every side of the conflict.
        chosen = [f for f in facts.facts if f.fact_id in used] or facts.facts
        return Answer(text=text, citations=_citations(chosen))

    def _fact_blocks(self, facts: FactSet) -> list[str]:
        blocks: list[str] = []
        for f in facts.facts:
            candidate = blocks + [json.dumps(f.to_prompt(), ensure_ascii=False)]
            if corpus_token_count(candidate) > self.max_corpus_tokens:
                log.warning("fact dropped to stay within corpus budget", extra={"rule": f.rule})
                continue
            blocks = candidate
        return blocks

    @staticmethod
    def _template_answer(facts: FactSet) -> str:
        lines = []
        for f in facts.facts:
            if f.status == "conflict":
                parts = "; ".join(f"{c.doc.title} says {c.value}" for c in f.claims)
                lines.append(f"The documents conflict on this point: {parts}.")
            elif f.status == "upcoming":
                c = f.claims[0]
                lines.append(f"Upcoming change from {c.doc.effective_date.isoformat()}: {c.statement} ({c.doc.title}).")
            else:
                c = f.claims[0]
                lines.append(f"{c.statement} ({c.doc.title}).")
        if facts.unanswered:
            lines.append(f"The documents do not cover: {facts.unanswered}. Please contact the HR team for that part.")
        return " ".join(lines)


def _dedupe(claims: list[Claim]) -> list[Claim]:
    seen, out = set(), []
    for c in claims:
        key = (c.doc.doc_id, c.value.lower())
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _citations(facts: list[Fact]) -> list[dict]:
    seen, out = set(), []
    for f in facts:
        for doc_id, locator in f.citations():
            if (doc_id, locator) not in seen:
                seen.add((doc_id, locator))
                out.append({"doc_id": doc_id, "locator": locator})
    return out


def _years_between(start: date, end: date) -> float:
    return round((end - start).days / 365.25, 2)
