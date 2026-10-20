"""Automatic extraction of public-holiday dates from calendar documents.

Calendar tables are read row by row: the first cell that parses as a date is
the holiday date. Tables (or sections) described as restricted or optional
holidays are skipped, because the office stays open on those days. If a
calendar has no usable table, dated lines in the text are used instead.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from app.ingestion.models import ExtractedDoc

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_MONTHS["sept"] = 9

_MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DMY = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH_RE}\.?,?\s+(\d{{4}})\b", re.I)
_MDY = re.compile(rf"\b{_MONTH_RE}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I)
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
             "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun"}
_EXCLUDE_WORDS = ("restricted", "optional", "floating")
_METADATA_KEYS = {"effective date", "version", "document id", "published", "last updated", "review date"}


def _month(name: str) -> int:
    key = name.lower().rstrip(".")
    return _MONTHS.get(key) or _MONTHS[key[:3]]


def parse_date_text(text: str) -> Optional[date]:
    text = text or ""
    for rx, order in ((_ISO, "ymd"), (_DMY, "dmy"), (_MDY, "mdy")):
        m = rx.search(text)
        if not m:
            continue
        try:
            if order == "ymd":
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if order == "dmy":
                return date(int(m.group(3)), _month(m.group(2)), int(m.group(1)))
            return date(int(m.group(3)), _month(m.group(1)), int(m.group(2)))
        except (ValueError, KeyError):
            continue
    return None


def _is_excluded(text: str) -> bool:
    low = (text or "").lower()
    return any(w in low for w in _EXCLUDE_WORDS)


def _holiday_name(cells: list[str], date_idx: int) -> str:
    names = [c for i, c in enumerate(cells) if i != date_idx and c and c.strip().lower() not in _WEEKDAYS]
    return names[-1] if names else "Holiday"


def extract_holidays(doc: ExtractedDoc) -> list[tuple[date, str]]:
    found: list[tuple[date, str]] = []
    heading: Optional[str] = None
    for block in doc.blocks:
        if block.kind == "heading":
            heading = block.text
            continue
        if block.kind != "table" or not block.rows:
            continue
        header_text = " ".join(block.rows[0])
        if _is_excluded(heading or "") or _is_excluded(header_text):
            continue
        for row in block.rows:
            if row and row[0].strip().lower() in _METADATA_KEYS:
                continue
            for idx, cell in enumerate(row):
                d = parse_date_text(cell)
                if d:
                    found.append((d, _holiday_name(row, idx)))
                    break

    if not found:  # fallback for calendars written as lists or prose
        heading = None
        for block in doc.blocks:
            if block.kind == "heading":
                heading = block.text
                continue
            if block.kind != "text" or _is_excluded(heading or "") or _is_excluded(block.text):
                continue
            d = parse_date_text(block.text)
            if d:
                found.append((d, _clean_name(block.text)))
    # de-duplicate, keep order by date
    unique = {(d, n) for d, n in found}
    return sorted(unique)


def _clean_name(text: str) -> str:
    text = _ISO.sub("", _MDY.sub("", _DMY.sub("", text)))
    words = [w for w in re.split(r"[\s,:;|–-]+", text) if w and w.lower() not in _WEEKDAYS]
    return " ".join(words).strip("- ") or "Holiday"
