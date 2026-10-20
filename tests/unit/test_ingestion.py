import json
import os
import shutil
from pathlib import Path

import pytest

from app.ingestion.chunker import chunk_document, render_table
from app.ingestion.holidays import extract_holidays, parse_date_text
from app.ingestion.models import Block, DocMeta, ExtractedDoc
from app.ingestion.pipeline import load_manifest, run_ingestion
from datetime import date


def chunks_of(kb, doc_id):
    return [c for c in kb.chunks if c.doc_id == doc_id]


def test_every_format_ingested(kb):
    assert kb.report.errors == {}
    assert len(kb.docs) == 15
    for doc_id in ("VOL-V1", "VOL-V2", "VOL-FAQ", "VOL-DE", "MEMO-SCAN"):  # md, docx, html, pdf, scanned pdf
        assert chunks_of(kb, doc_id), doc_id


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_scanned_pdf_is_ocrd(kb):
    assert kb.report.ocr_documents == ["MEMO-SCAN"]
    text = " ".join(c.text for c in chunks_of(kb, "MEMO-SCAN"))
    assert "3 days per week" in text
    assert chunks_of(kb, "MEMO-SCAN")[0].locator == "p. 1"  # no headings -> page locator


def test_headings_become_locators(kb):
    assert chunks_of(kb, "VOL-DE")[0].locator == "1. Volunteer days"          # bold PDF heading
    assert chunks_of(kb, "VOL-FAQ")[0].locator == "How many volunteer days do I get?"  # html h3
    assert chunks_of(kb, "VOL-V2")[0].locator == "1. Volunteer days"           # docx Heading 1


def test_pdf_running_header_and_title_dropped(kb):
    text = chunks_of(kb, "VOL-DE")[0].text
    assert "Internal" not in text and "Leave Addendum" not in text


def test_metadata_tables_dropped(kb):
    for c in kb.chunks:
        assert "Document ID" not in c.text


def test_docx_table_keeps_rows(kb):
    table_chunk = next(c for c in chunks_of(kb, "VOL-V2") if c.locator == "2. Summary table")
    assert "| Item | Value |" in table_chunk.text and "| Volunteer days | 3 |" in table_chunk.text


def test_holidays_extracted_and_restricted_excluded(kb):
    assert [d.isoformat() for d in kb.holidays_by_doc["CAL-IN-2026"]] == [
        "2026-01-26", "2026-10-20", "2026-11-09", "2026-12-25"]
    assert kb.holiday_names[("CAL-IN-2026", date(2026, 11, 9))] == "Deepavali"
    assert len(kb.holidays_by_doc["CAL-DE-2026"]) == 3 and len(kb.holidays_by_doc["CAL-US-2026"]) == 2


@pytest.mark.parametrize("text,expected", [
    ("1 January 2026", date(2026, 1, 1)), ("January 19, 2026", date(2026, 1, 19)),
    ("2026-07-03", date(2026, 7, 3)), ("3rd Sept 2026", date(2026, 9, 3)), ("no date", None),
])
def test_parse_date_text(text, expected):
    assert parse_date_text(text) == expected


def test_holiday_list_fallback_when_no_table():
    meta = DocMeta("C", "c.md", "C", "calendar", ("IN",), date(2026, 1, 1), "all")
    doc = ExtractedDoc(meta, [Block("heading", "Holidays"), Block("text", "- 2 October 2026: Gandhi Jayanti"),
                              Block("heading", "Optional holidays"), Block("text", "- 4 March 2026: Holi")], False)
    assert extract_holidays(doc) == [(date(2026, 10, 2), "Gandhi Jayanti")]


def test_long_table_split_repeats_header():
    rows = [["Date", "Holiday"]] + [[f"{i} May 2026", f"Holiday number {i} with a long descriptive name"]
                                    for i in range(1, 60)]
    meta = DocMeta("T", "t.md", "T", "calendar", ("IN",), date(2026, 1, 1), "all")
    chunks = chunk_document(ExtractedDoc(meta, [Block("heading", "Dates"), Block("table", rows=rows)], False),
                            max_tokens=200)
    assert len(chunks) > 1
    for c in chunks:
        assert "| Date | Holiday |" in c.text and c.locator == "Dates" and c.token_count <= 200


def test_render_table_pads_ragged_rows():
    assert render_table([["a", "b"], ["1"]]).splitlines()[-1] == "| 1 |  |"


def test_malformed_manifest_entries_skipped(tmp_path):
    (tmp_path / "m.json").write_text(json.dumps([
        {"doc_id": "OK", "file": "a.md", "title": "A", "doc_type": "policy", "regions": ["GLOBAL"],
         "effective_date": "2026-01-01", "access_level": "all"},
        {"doc_id": "BAD-DATE", "file": "b.md", "doc_type": "policy", "regions": ["GLOBAL"],
         "effective_date": "soon", "access_level": "all"},
        {"doc_id": "BAD-ACCESS", "file": "c.md", "doc_type": "policy", "regions": ["GLOBAL"],
         "effective_date": "2026-01-01", "access_level": "everyone"},
        {"file": "missing-id.md"},
    ]))
    docs, skipped = load_manifest(str(tmp_path / "m.json"))
    assert [d.doc_id for d in docs] == ["OK"] and skipped == 3


def test_bad_files_are_reported_not_fatal(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "good.md").write_text("# T\n\n## S\n\nEmployees receive 2 volunteer days per year.\n")
    (corpus / "broken.pdf").write_bytes(b"%PDF-1.4 not really a pdf")
    (corpus / "empty.md").write_text("")
    entries = [
        {"doc_id": d, "file": f, "title": d, "doc_type": "policy", "regions": ["GLOBAL"],
         "effective_date": "2026-01-01", "access_level": "all"}
        for d, f in [("GOOD", "good.md"), ("BROKEN", "broken.pdf"), ("EMPTY", "empty.md"),
                     ("MISSING", "nope.md"), ("ESCAPE", "../etc/passwd")]
    ]
    (tmp_path / "manifest.json").write_text(json.dumps(entries))
    kb = run_ingestion(str(corpus), str(tmp_path / "manifest.json"), None, None, ocr=None)
    assert list(kb.docs) == ["GOOD"]
    assert set(kb.report.errors) == {"BROKEN", "EMPTY", "MISSING", "ESCAPE"}


REAL_DATA = Path(os.environ.get("REAL_DATA_DIR", "data"))


@pytest.mark.skipif(not (REAL_DATA / "manifest.json").exists() or shutil.which("tesseract") is None,
                    reason="assessment data folder not present")
def test_real_corpus_ingests_completely():
    kb = run_ingestion(str(REAL_DATA / "corpus"), str(REAL_DATA / "manifest.json"), None, None)
    manifest = json.loads((REAL_DATA / "manifest.json").read_text())
    assert kb.report.errors == {}
    assert len(kb.docs) == len(manifest)
    assert kb.report.ocr_documents, "expected at least one OCR'd document"
    calendars = [m["doc_id"] for m in manifest if m["doc_type"] == "calendar"]
    assert all(kb.holidays_by_doc.get(c) for c in calendars)
    assert max(c.token_count for c in kb.chunks) <= 400
