"""Personal data helpers: email masking and scrubbing of free text.

Personal data in this service means full_name, email and phone (Section 7.4).
"""
from __future__ import annotations

import re
import threading
from typing import Iterable, Optional

_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
# Phone-like runs: optional +, then 8+ digits allowing spaces, dots, dashes and brackets.
_PHONE_RE = re.compile(r"(?<![\w-])\+?\(?\d[\d\s().-]{6,}\d(?![\w-])")


def mask_email(email: str) -> str:
    """r***@veltrix.example"""
    if "@" not in email or not email:
        return "***"
    local, domain = email.split("@", 1)
    return f"{local[:1]}***@{domain}"


def _mask_email_match(m: re.Match) -> str:
    full = m.group(0)
    local = full.split("@", 1)[0]
    if local.endswith("***") or local.startswith("***"):
        return full
    return f"{m.group(1)}***@{m.group(2)}"


def _phone_sub(m: re.Match) -> str:
    text = m.group(0)
    digits = re.sub(r"\D", "", text)
    if len(digits) < 8:
        return text
    # Do not mangle ISO dates or date ranges such as "2026-11-09 2026-11-13".
    if re.search(r"\d{4}-\d{2}-\d{2}", text):
        return text
    if not text.lstrip().startswith(("+", "(")) and len(digits) < 10:
        return text
    return "[phone removed]"


class NameRegistry:
    """Known employee full names, loaded from the database, used to scrub logs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pattern: re.Pattern | None = None
        self._by_name: dict[str, str] = {}

    def load(self, pairs: Iterable[tuple[str, str]]) -> None:
        by_name = {name.strip().lower(): emp_id for emp_id, name in pairs if name and name.strip()}
        pattern = None
        if by_name:
            alts = sorted((re.escape(n) for n in by_name), key=len, reverse=True)
            pattern = re.compile(r"\b(" + "|".join(alts) + r")\b", re.IGNORECASE)
        with self._lock:
            self._by_name, self._pattern = by_name, pattern

    def replace_names(self, text: str, visible_ids: Optional[set[str]] = None) -> str:
        """Known names become employee IDs. With `visible_ids`, only names of employees the
        requester may see are mapped; anyone else becomes "[name]" (no name-to-ID oracle)."""
        pattern = self._pattern
        if not pattern:
            return text

        def sub(m: re.Match) -> str:
            emp_id = self._by_name.get(m.group(0).lower())
            if emp_id is None or (visible_ids is not None and emp_id not in visible_ids):
                return "[name]"
            return emp_id

        return pattern.sub(sub, text)

    def mentions_other_than(self, text: str, employee_id: str) -> bool:
        pattern = self._pattern
        if not pattern:
            return False
        return any(self._by_name.get(m.group(0).lower()) != employee_id for m in pattern.finditer(text))


NAMES = NameRegistry()


def scrub_contact_details(text: str) -> str:
    """Mask emails and remove phone numbers; keeps names."""
    text = _EMAIL_RE.sub(_mask_email_match, text)
    return _PHONE_RE.sub(_phone_sub, text)


def scrub_all(text: str) -> str:
    """Logs and LLM history: names become employee IDs, emails masked, phones removed."""
    return scrub_contact_details(NAMES.replace_names(text))


_KNOWN_EMAILS: set[str] = set()


def load_known_emails(emails: Iterable[str]) -> None:
    _KNOWN_EMAILS.clear()
    _KNOWN_EMAILS.update(e.strip().lower() for e in emails if e)


def _mask_personal_email(m: re.Match) -> str:
    full = m.group(0)
    local = full.split("@", 1)[0]
    if "***" in local:
        return full
    # Employee mailboxes are masked; shared role mailboxes from the corpus (hr@, finance@) are not personal data.
    if full.lower() in _KNOWN_EMAILS or "." in local:
        return f"{m.group(1)}***@{m.group(2)}"
    return full


def scrub_answer(text: str) -> str:
    """Final pass over every answer: never a phone number, employee emails masked."""
    text = _EMAIL_RE.sub(_mask_personal_email, text)
    return _PHONE_RE.sub(_phone_sub, text)
