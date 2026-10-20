"""Ingestion: manifest + corpus -> extracted blocks -> chunks -> hybrid index + holiday table.

Runs from scratch on every start, so a replaced ./data folder is always picked
up and nothing depends on a previously built index.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from app.db import repositories as repo
from app.db.database import connection
from app.ingestion import extractors
from app.ingestion.chunker import chunk_document
from app.ingestion.holidays import extract_holidays
from app.ingestion.models import Chunk, DocMeta, ExtractedDoc
from app.retrieval.index import Embedder, HybridIndex

log = logging.getLogger(__name__)

VALID_DOC_TYPES = {"policy", "addendum", "memo", "handbook", "calendar", "faq", "wiki"}
VALID_ACCESS = {"all", "managers", "hr"}


@dataclass
class IngestionReport:
    documents: int = 0
    chunks: int = 0
    ocr_documents: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    skipped_manifest_entries: int = 0
    holidays: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    dense_retrieval: bool = False


@dataclass
class KnowledgeBase:
    docs: dict[str, DocMeta]
    chunks: list[Chunk]
    index: HybridIndex
    holidays_by_doc: dict[str, list[date]]
    report: IngestionReport
    holiday_names: dict[tuple[str, date], str] = field(default_factory=dict)

    def chunk(self, chunk_id: str) -> Chunk:
        return self._by_id[chunk_id]

    def __post_init__(self) -> None:
        self._by_id = {c.chunk_id: c for c in self.chunks}


def load_manifest(path: str) -> tuple[list[DocMeta], int]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("manifest.json must be a JSON array")
    docs, skipped, seen = [], 0, set()
    for i, entry in enumerate(raw):
        try:
            regions = entry["regions"]
            if isinstance(regions, str):
                regions = [regions]
            meta = DocMeta(
                doc_id=str(entry["doc_id"]).strip(),
                file=str(entry["file"]).strip(),
                 title=str(entry.get("title") or entry["doc_id"]).strip(),
                doc_type=str(entry["doc_type"]).strip().lower(),
                regions=tuple(str(r).strip().upper() for r in regions),
                effective_date=date.fromisoformat(str(entry["effective_date"]).strip()),
                access_level=str(entry["access_level"]).strip().lower(),
            )
            if not meta.doc_id or meta.doc_id in seen:
                raise ValueError("missing or duplicate doc_id")
            if meta.access_level not in VALID_ACCESS:
                raise ValueError(f"unknown access_level {meta.access_level!r}")
            if meta.doc_type not in VALID_DOC_TYPES:
                log.warning("manifest entry has unknown doc_type; treated as low authority",
                            extra={"doc_id": meta.doc_id, "doc_type": meta.doc_type})
            seen.add(meta.doc_id)
            docs.append(meta)
        except (KeyError, ValueError, TypeError) as exc:
            skipped += 1
            log.error("skipping malformed manifest entry", extra={"index": i, "error": type(exc).__name__,
                                                                   "detail": str(exc)[:200]})
    return docs, skipped


def extract_document(meta: DocMeta, corpus_dir: Path, ocr: Optional[extractors.OcrFn]) -> ExtractedDoc:
    path = (corpus_dir / meta.file).resolve()
    if corpus_dir.resolve() not in path.parents:
        raise ValueError("file path escapes CORPUS_DIR")
    if not path.is_file():
        raise FileNotFoundError(meta.file)
    suffix = path.suffix.lower()
    used_ocr, paginated = False, False
    if suffix == ".pdf":
        blocks, used_ocr = extractors.extract_pdf(path, ocr)
        paginated = True
    elif suffix == ".docx":
        blocks = extractors.extract_docx(path)
    elif suffix in (".html", ".htm"):
        blocks = extractors.extract_html(path)
    elif suffix in (".md", ".markdown"):
        blocks = extractors.extract_markdown(path)
    elif suffix == ".txt":
        blocks = extractors.extract_plain_text(path)
    else:
        raise ValueError(f"unsupported file type {suffix}")
    has_headings = any(b.kind == "heading" for b in blocks)
    if not any(b.kind != "heading" for b in blocks):
        raise ValueError("no text extracted")
    return ExtractedDoc(meta=meta, blocks=blocks, paginated=paginated, used_ocr=used_ocr, has_headings=has_headings)


def run_ingestion(
    corpus_dir: str,
    manifest_path: str,
    db_path: Optional[str],
    embedder_factory: Optional[Callable[[], Optional[Embedder]]] = None,
    ocr: Optional[extractors.OcrFn] = extractors.tesseract_ocr,
) -> KnowledgeBase:
    started = time.monotonic()
    report = IngestionReport()
    metas, report.skipped_manifest_entries = load_manifest(manifest_path)
    corpus = Path(corpus_dir)

    docs: dict[str, DocMeta] = {}
    chunks: list[Chunk] = []
    holidays_by_doc: dict[str, list[date]] = {}
    holiday_rows: list[tuple[str, str, str]] = []
    holiday_names: dict[tuple[str, date], str] = {}
    for meta in metas:
        try:
            extracted = extract_document(meta, corpus, ocr)
        except Exception as exc:
            report.errors[meta.doc_id] = f"{type(exc).__name__}: {str(exc)[:200]}"
            log.error("document ingestion failed", extra={"doc_id": meta.doc_id, "error": type(exc).__name__})
            continue
        docs[meta.doc_id] = meta
        if extracted.used_ocr:
            report.ocr_documents.append(meta.doc_id)
        doc_chunks = chunk_document(extracted)
        # identical repeated passages inside one document add nothing for retrieval
        seen_text: set[str] = set()
        for c in doc_chunks:
            if c.text not in seen_text:
                seen_text.add(c.text)
                chunks.append(c)
        if meta.doc_type == "calendar":
            hols = extract_holidays(extracted)
            holidays_by_doc[meta.doc_id] = sorted({d for d, _ in hols})
            for d, n in hols:
                holiday_names.setdefault((meta.doc_id, d), n)
            report.holidays[meta.doc_id] = len(hols)
            holiday_rows.extend((meta.doc_id, d.isoformat(), n) for d, n in hols)
            if not hols:
                log.warning("calendar document yielded no holiday dates", extra={"doc_id": meta.doc_id})

    if db_path:
        with connection(db_path) as conn:
            repo.replace_holidays(conn, holiday_rows)

    embedder = None
    if embedder_factory is not None:
        try:
            embedder = embedder_factory()
        except Exception:
            log.warning("embedding model unavailable; lexical retrieval only", exc_info=True)
    index = HybridIndex(chunks, {d.doc_id: d.title for d in docs.values()}, embedder)

    report.documents = len(docs)
    report.chunks = len(chunks)
    report.dense_retrieval = index.dense_enabled
    report.seconds = round(time.monotonic() - started, 2)
    log.info("ingestion finished", extra={
        "documents": report.documents, "chunks": report.chunks, "ocr_documents": report.ocr_documents,
        "failed_documents": sorted(report.errors), "holidays": report.holidays,
        "dense_retrieval": report.dense_retrieval, "seconds": report.seconds,
    })
    return KnowledgeBase(docs=docs, chunks=chunks, index=index, holidays_by_doc=holidays_by_doc, report=report,
                         holiday_names=holiday_names)

