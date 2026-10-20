"""Format-specific text extraction that keeps structure: headings, tables, pages.

Each extractor returns Blocks in reading order. Tables keep their rows and
columns. The document-metadata table at the top of every corpus file is
dropped, because the manifest is the authoritative metadata source.
"""
from __future__ import annotations

import io
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

from app.ingestion.models import Block

log = logging.getLogger(__name__)

OcrFn = Callable[[bytes], str]


def _clean(text: str) -> str:
    return re.sub(r"[ \t ]+", " ", text or "").strip()


def is_metadata_table(rows: list[list[str]]) -> bool:
    """The per-document header table ("Document ID | ...", or Markdown "Field | Value")."""
    for row in rows[:3]:
        first = (row[0] if row else "") or ""
        first = " ".join(first.split()).lower()
        if first.startswith("document id") or first.startswith("doc id"):
            return True
    return False


def _normalise_rows(raw_rows: list[list[Optional[str]]]) -> list[list[str]]:
    rows = []
    for raw in raw_rows:
        row = [_clean((c or "").replace("\n", " ")) for c in raw]
        if any(row):
            rows.append(row)
    return rows


def _ocr_lines_to_blocks(text: str, page: int) -> list[Block]:
    blocks = []
    for para in re.split(r"\n\s*\n", text):
        lines = []
        for line in para.splitlines():
            line = line.strip()
            if not line:
                continue
            alnum = sum(ch.isalnum() or ch.isspace() for ch in line)
            if alnum / max(len(line), 1) < 0.75:
                continue  # OCR noise from signatures and stamps
            lines.append(line)
        if lines:
            blocks.append(Block("text", " ".join(lines), page=page))
    return blocks


# ------------------------------------------------------------------ PDF
def extract_pdf(path: Path, ocr: Optional[OcrFn]) -> tuple[list[Block], bool]:
    import pymupdf

    doc = pymupdf.open(str(path))
    used_ocr = False
    sizes: Counter = Counter()
    for page in doc:
        for b in page.get_text("dict")["blocks"]:
            for line in b.get("lines", []):
                for span in line["spans"]:
                    if span["text"].strip():
                        sizes[round(span["size"], 1)] += len(span["text"])
    body_size = sizes.most_common(1)[0][0] if sizes else 10.0
    title_size = max(sizes) if sizes else body_size

    blocks: list[Block] = []
    for pno, page in enumerate(doc, start=1):
        page_text = page.get_text().strip()
        if len(page_text) < 30 and page.get_images():
            if ocr is None:
                raise RuntimeError("page has no text layer and OCR is unavailable")
            pix = page.get_pixmap(dpi=300)
            blocks.extend(_ocr_lines_to_blocks(ocr(pix.tobytes("png")), pno))
            used_ocr = True
            continue

        items: list[tuple[float, Block]] = []
        table_rects = []
        for t in page.find_tables().tables:
            rect = pymupdf.Rect(t.bbox)
            table_rects.append(rect)
            rows = _normalise_rows(t.extract())
            if rows and not is_metadata_table(rows):
                items.append((rect.y0, Block("table", rows=rows, page=pno)))

        height = page.rect.height
        pending_bullet = False
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            rect = pymupdf.Rect(b["bbox"])
            centre = pymupdf.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
            if any(tr.contains(centre) for tr in table_rects):
                continue
            spans = [s for line in b.get("lines", []) for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            max_size = max(round(s["size"], 1) for s in spans)
            is_bold = all(("bold" in s["font"].lower()) or (s["flags"] & 16) for s in spans)
            text = _clean(" ".join("".join(s["text"] for s in line["spans"]) for line in b["lines"]))
            if not text:
                continue
            in_margin = rect.y1 < height * 0.09 or rect.y0 > height * 0.93
            if max_size < body_size * 0.9 and in_margin:
                continue  # running header / footer / page number
            if pno == 1 and max_size >= title_size and max_size > body_size * 1.3:
                continue  # document title; the manifest title is used instead
            if text == "•":
                pending_bullet = True
                continue
            if is_bold and max_size > body_size * 1.1 and len(text) < 120:
                items.append((rect.y0, Block("heading", text, page=pno, level=1)))
            else:
                if text.startswith("• "):
                    text = "- " + text[2:]
                elif pending_bullet:
                    text = "- " + text
                items.append((rect.y0, Block("text", text, page=pno)))
            pending_bullet = False
        items.sort(key=lambda it: it[0])
        blocks.extend(b for _, b in items)
    return _merge_continuations(blocks), used_ocr


def _merge_continuations(blocks: list[Block]) -> list[Block]:
    """Join a paragraph split by a page break (previous text does not end a sentence)."""
    out: list[Block] = []
    for b in blocks:
        prev = out[-1] if out else None
        if (
            prev is not None and b.kind == "text" and prev.kind == "text"
            and prev.page is not None and b.page == prev.page + 1
            and not prev.text.rstrip().endswith((".", ":", "?", "!"))
            and not b.text.startswith("- ")
        ):
            prev.text = f"{prev.text} {b.text}"
            continue
        out.append(b)
    return out


# ------------------------------------------------------------------ DOCX
def extract_docx(path: Path) -> list[Block]:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(str(path))
    blocks: list[Block] = []
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(child, document)
            text = _clean(para.text)
            if not text:
                continue
            style = (para.style.name if para.style is not None else "") or ""
            low = style.lower()
            if low == "title":
                continue
            if low.startswith("heading"):
                level = int(re.sub(r"\D", "", low) or 1)
                blocks.append(Block("heading", text, level=level))
            elif "list" in low:
                blocks.append(Block("text", f"- {text}"))
            else:
                runs = [r for r in para.runs if r.text.strip()]
                if runs and all(r.bold for r in runs) and len(text) < 100 and not text.endswith("."):
                    blocks.append(Block("heading", text, level=2))
                else:
                    blocks.append(Block("text", text))
        elif tag == "tbl":
            table = Table(child, document)
            rows = []
            for row in table.rows:
                seen, cells = set(), []
                for cell in row.cells:
                    if id(cell._tc) in seen:
                        continue  # merged cells repeat in python-docx
                    seen.add(id(cell._tc))
                    cells.append(_clean(cell.text.replace("\n", " ")))
                rows.append(cells)
            rows = [r for r in rows if any(r)]
            if rows and not is_metadata_table(rows):
                blocks.append(Block("table", rows=rows))
    return blocks


# ------------------------------------------------------------------ HTML
def extract_html(path: Path) -> list[Block]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "head", "noscript"]):
        tag.decompose()
    root = soup.body or soup
    blocks: list[Block] = []
    wanted = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table", "pre", "blockquote"]
    for el in root.find_all(wanted):
        if el.name != "table" and el.find_parent("table"):
            continue
        if el.name == "p" and el.find_parent(["li", "blockquote"]):
            continue
        if el.name == "table":
            if el.find_parent("table"):
                continue
            rows = _normalise_rows([[c.get_text(" ") for c in tr.find_all(["th", "td"])] for tr in el.find_all("tr")])
            if rows and not is_metadata_table(rows):
                blocks.append(Block("table", rows=rows))
            continue
        text = _clean(el.get_text(" "))
        if not text:
            continue
        if el.name == "h1":
            continue  # page title; the manifest title is used instead
        if el.name.startswith("h"):
            blocks.append(Block("heading", text, level=int(el.name[1])))
        elif el.name == "li":
            blocks.append(Block("text", f"- {text}"))
        else:
            blocks.append(Block("text", text))
    return blocks


# ------------------------------------------------------------------ Markdown / text
_MD_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def extract_markdown(path: Path) -> list[Block]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    blocks: list[Block] = []
    para: list[str] = []
    table: list[list[str]] = []
    seen_title = False

    def flush_para():
        if para:
            blocks.append(Block("text", _clean(" ".join(para))))
            para.clear()

    def flush_table():
        if table:
            rows = [r for r in table if any(r)]
            if rows and not is_metadata_table(rows):
                blocks.append(Block("table", rows=[list(r) for r in rows]))
            table.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|"):
            flush_para()
            if _MD_TABLE_SEP.match(stripped):
                continue
            table.append([_clean(c) for c in stripped.strip("|").split("|")])
            continue
        flush_table()
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para()
            level, text = len(m.group(1)), _clean(m.group(2).strip("# "))
            if level == 1 and not seen_title:
                seen_title = True
                continue
            blocks.append(Block("heading", text, level=level))
        elif re.match(r"^([-*+]|\d+[.)])\s+", stripped):
            flush_para()
            blocks.append(Block("text", "- " + _clean(re.sub(r"^([-*+]|\d+[.)])\s+", "", stripped))))
        elif not stripped:
            flush_para()
        else:
            para.append(stripped)
    flush_para()
    flush_table()
    return blocks


def extract_plain_text(path: Path) -> list[Block]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return [Block("text", _clean(p.replace("\n", " "))) for p in re.split(r"\n\s*\n", text) if p.strip()]


def tesseract_ocr(png_bytes: bytes) -> str:
    import pytesseract
    from PIL import Image

    return pytesseract.image_to_string(Image.open(io.BytesIO(png_bytes)), timeout=120)
