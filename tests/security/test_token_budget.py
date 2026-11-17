"""Section 4.3: never more than 3,000 cl100k_base tokens of corpus text per LLM call."""
from datetime import date

import pytest

from app.ingestion.models import Chunk, DocMeta
from app.ingestion.pipeline import IngestionReport, KnowledgeBase
from app.llm.client import CorpusBudgetExceeded, corpus_token_count
from app.llm.tokens import count_tokens
from app.orchestration.rag import RagEngine
from app.retrieval.index import HybridIndex
from tests.conftest import employee
from tests.fakes import make_client, plan


def test_client_refuses_over_budget():
    client, provider = make_client(lambda m: plan())
    blocks = ["leave " * 3100]
    with pytest.raises(CorpusBudgetExceeded):
        client.complete_json(purpose="t", system="s", user="u", schema={}, corpus_blocks=blocks)
    assert provider.calls == []  # nothing was sent


def test_client_counts_with_cl100k():
    assert count_tokens("hello world") == 2


def test_selection_stays_within_budget_on_large_corpus(conn):
    """Many relevant chunks: retrieval must stop adding sources before the budget."""
    docs, chunks = {}, []
    for i in range(80):
        meta = DocMeta(f"D{i}", f"d{i}.md", f"Doc {i}", "policy", ("GLOBAL",), date(2025, 1, 1), "all")
        docs[meta.doc_id] = meta
        text = "## Sick leave\n" + ("Employees receive 12 days of paid sick leave each year. " * 12)
        chunks.append(Chunk(f"D{i}#0", meta.doc_id, "Sick leave", "Sick leave", text, None, count_tokens(text)))
    kb = KnowledgeBase(docs=docs, chunks=chunks, index=HybridIndex(chunks, {d: m.title for d, m in docs.items()}),
                       holidays_by_doc={}, report=IngestionReport())
    engine = RagEngine(kb, None, 3000)
    sources = engine.select_sources(employee(conn, "E201"), ["sick leave days"], date(2026, 10, 16), True)
    assert sources
    assert corpus_token_count([s.render() for s in sources]) <= 3000


def test_every_call_in_a_chat_is_logged_and_within_budget(conn, kb, caplog):
    client, provider = make_client(lambda m: plan(search_query="How many volunteer days do I get?"))
    engine = RagEngine(kb, client, 3000)
    caplog.set_level("INFO")
    facts = engine.gather_facts(question="volunteer days", queries=["volunteer days"],
                                requester=employee(conn, "E201"), reference_date=date(2026, 10, 16),
                                today=date(2026, 10, 16), deadline=None, request_id="r1")
    engine.compose(question="volunteer days", facts=facts, requester=employee(conn, "E201"),
                   today=date(2026, 10, 16), reference_date=date(2026, 10, 16), tool_data=None,
                   deadline=None, request_id="r1")
    llm_logs = [r for r in caplog.records if getattr(r, "event", "") == "llm_call"]
    assert [r.purpose for r in llm_logs] == ["extract_claims", "compose_answer"]
    assert all(0 < r.corpus_tokens <= 3000 for r in llm_logs)
