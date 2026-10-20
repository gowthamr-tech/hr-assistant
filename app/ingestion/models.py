"""Data types shared by ingestion, retrieval and policy resolution."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class DocMeta:
    """One manifest entry. The manifest is the only source of document metadata."""
    doc_id: str
    file: str
    title: str
    doc_type: str
    regions: tuple[str, ...]
    effective_date: date
    access_level: str

    @property
    def is_global(self) -> bool:
        return "GLOBAL" in self.regions

    def applies_to_region(self, region: str) -> bool:
        return self.is_global or region in self.regions


@dataclass
class Block:
    """A structural unit extracted from a document, in reading order."""
    kind: str  # "heading" | "text" | "table"
    text: str = ""
    page: Optional[int] = None
    level: int = 0
    rows: list[list[str]] = field(default_factory=list)  # for tables: rows[0] is the header


@dataclass
class ExtractedDoc:
    meta: DocMeta
    blocks: list[Block]
    paginated: bool
    used_ocr: bool = False
    has_headings: bool = False


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    heading: Optional[str]
    locator: str
    text: str  # what the LLM sees (section heading + body)
    page: Optional[int]
    token_count: int
