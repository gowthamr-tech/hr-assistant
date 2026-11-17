"""Structure-aware chunking: one chunk per section, split only when a section is long.

A chunk never mixes two sections, so its locator (nearest heading, or page)
is exact. Tables are rendered as Markdown with their header row; a table that
must be split repeats the header in every piece so values stay unambiguous.
"""
from __future__ import annotations

from typing import Optional

from app.ingestion.models import Block, Chunk, ExtractedDoc
from app.llm.tokens import count_tokens

MAX_CHUNK_TOKENS = 380


def render_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    padded = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(padded[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in padded[1:]]
    return "\n".join(lines)


def _table_pieces(rows: list[list[str]], budget: int) -> list[str]:
    whole = render_table(rows)
    if count_tokens(whole) <= budget or len(rows) <= 2:
        return [whole]
    header, body = rows[0], rows[1:]
    pieces, current = [], []
    for row in body:
        candidate = render_table([header] + current + [row])
        if current and count_tokens(candidate) > budget:
            pieces.append(render_table([header] + current))
            current = [row]
        else:
            current.append(row)
    if current:
        pieces.append(render_table([header] + current))
    return pieces


def _locator(doc: ExtractedDoc, heading: Optional[str], page: Optional[int]) -> str:
    if heading:
        return heading
    if doc.paginated and page:
        return f"p. {page}"
    return doc.meta.title


def chunk_document(doc: ExtractedDoc, max_tokens: int = MAX_CHUNK_TOKENS) -> list[Chunk]:
    # 1. group blocks into sections keyed by their nearest heading
    sections: list[tuple[Optional[str], list[Block]]] = []
    heading: Optional[str] = None
    current: list[Block] = []
    for block in doc.blocks:
        if block.kind == "heading":
            if current:
                sections.append((heading, current))
            heading, current = block.text, []
        else:
            current.append(block)
    if current:
        sections.append((heading, current))

    # 2. pack each section's blocks into chunks under the token limit
    chunks: list[Chunk] = []
    for heading, blocks in sections:
        prefix = f"## {heading}\n" if heading else ""
        budget = max_tokens - count_tokens(prefix)
        parts: list[tuple[str, Optional[int]]] = []
        for b in blocks:
            if b.kind == "table":
                parts.extend((piece, b.page) for piece in _table_pieces(b.rows, budget))
            else:
                parts.append((b.text, b.page))

        buf: list[str] = []
        buf_page: Optional[int] = None
        buf_tokens = 0

        def emit():  # called only within this iteration, so loop variables are current
            nonlocal buf, buf_page, buf_tokens
            if not buf:
                return
            text = prefix + "\n".join(buf)  # noqa: B023
            idx = len(chunks)
            chunks.append(Chunk(
                chunk_id=f"{doc.meta.doc_id}#{idx}",
                doc_id=doc.meta.doc_id,
                heading=heading,  # noqa: B023
                locator=_locator(doc, heading, buf_page),  # noqa: B023
                text=text,
                page=buf_page,
                token_count=count_tokens(text),
            ))
            buf, buf_page, buf_tokens = [], None, 0

        for text, page in parts:
            t = count_tokens(text) + 1
            # page-located chunks (no heading) must not straddle pages, or "p. N" would be wrong
            page_break = heading is None and buf_page is not None and page is not None and page != buf_page
            if buf and (buf_tokens + t > budget or page_break):
                emit()
            if not buf:
                buf_page = page
            if t > budget:  # a single over-long paragraph: split on sentences
                for sentence_group in _split_long_text(text, budget):
                    buf.append(sentence_group)
                    buf_tokens = count_tokens(sentence_group)
                    emit()
                    buf_page = page
                buf_page = None
                continue
            buf.append(text)
            buf_tokens += t
        emit()
    return chunks


def _split_long_text(text: str, budget: int) -> list[str]:
    import re

    sentences = re.split(r"(?<=[.!?])\s+", text)
    groups, current = [], ""
    for s in sentences:
        candidate = f"{current} {s}".strip()
        if current and count_tokens(candidate) > budget:
            groups.append(current)
            current = s
        else:
            current = candidate
    if current:
        groups.append(current)
    return groups
