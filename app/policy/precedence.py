"""Section 5.1 precedence, implemented in code (the LLM only extracts claims).

A Claim is one value one document states for one rule. For every rule:
  1. Authority: policy/addendum/memo/handbook/calendar outrank faq/wiki.
  2. Scope: region-specific outranks GLOBAL.
  3. Recency: later effective_date outranks earlier.
Each test runs only on the claims that survived the previous one. If the
survivors still state different values, the conflict is unresolved and every
surviving value is reported.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from app.ingestion.models import DocMeta

HIGH_AUTHORITY = frozenset({"policy", "addendum", "memo", "handbook", "calendar"})


def authority_rank(doc: DocMeta) -> int:
    return 1 if doc.doc_type in HIGH_AUTHORITY else 0


def scope_rank(doc: DocMeta) -> int:
    return 0 if doc.is_global else 1


def precedence_key(doc: DocMeta) -> tuple[int, int, date]:
    return (authority_rank(doc), scope_rank(doc), doc.effective_date)


@dataclass(frozen=True)
class Claim:
    rule: str
    value: str
    statement: str
    doc: DocMeta
    locator: str
    source_label: str = ""


@dataclass
class Resolution:
    rule: str
    winners: list[Claim]            # surviving claims after the three tests
    conflict: bool                  # survivors disagree -> report all values
    decided_by: Optional[str]       # "authority" | "scope" | "recency" | None
    losers: list[Claim] = field(default_factory=list)

    @property
    def values(self) -> list[str]:
        out: list[str] = []
        for c in self.winners:
            if not any(same_value(c.value, v) for v in out):
                out.append(c.value)
        return out


@dataclass(frozen=True)
class UpcomingChange:
    rule: str
    claim: Claim
    effective_date: date


_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")


def normalise_value(value: str) -> tuple:
    """Two values are 'the same' if they state the same numbers (and months), else same words."""
    low = (value or "").lower()
    nums = tuple(n.replace(",", "") for n in _NUM.findall(low))
    months = tuple(m for m in _MONTHS if m in low)
    if nums:
        return ("n", nums, months)
    words = tuple(sorted(set(re.findall(r"[a-z]+", low)) - {"the", "a", "an", "of", "per", "up", "to"}))
    return ("w", words)


_NEGATIONS = {"no", "not", "never", "none", "cannot", "without", "nil"}
_FILLER = {"the", "a", "an", "of", "per", "up", "to", "is", "are", "be", "in", "on", "for", "and", "or", "each",
           "every", "at", "by", "it", "can", "may", "must"}


def _words(value: str) -> tuple[frozenset, bool]:
    low = (value or "").lower().replace("can't", "cannot").replace("n't", " not")
    words = set()
    for w in re.findall(r"[a-z]+", low):
        for suf in ("ly", "s"):
            if w.endswith(suf) and len(w) > len(suf) + 3:
                w = w[: -len(suf)]
        words.add(w)
    return frozenset(words - _FILLER - _NEGATIONS), bool(words & _NEGATIONS)


def same_value(a: str, b: str) -> bool:
    """Numbers decide when present ("12 days" == "12 working days"). Otherwise compare wording:
    same negation and mostly the same content words ("earned monthly" == "earned each month")."""
    na, nb = normalise_value(a), normalise_value(b)
    if na[0] == "n" or nb[0] == "n":
        return na == nb
    (wa, neg_a), (wb, neg_b) = _words(a), _words(b)
    if neg_a != neg_b:
        return False
    if not wa or not wb:
        return wa == wb
    return len(wa & wb) / len(wa | wb) >= 0.5


def _values_by_doc(claims: list[Claim]) -> dict[str, list[str]]:
    by_doc: dict[str, list[str]] = {}
    for c in claims:
        by_doc.setdefault(c.doc.doc_id, []).append(c.value)
    return by_doc


def _covers(xs: list[str], ys: list[str]) -> bool:
    return all(any(same_value(x, y) for y in ys) for x in xs)


def disagree(claims: list[Claim]) -> bool:
    """Documents disagree when they state different values for the rule. Several values from ONE
    document (e.g. one rate per service band) are complementary, not a conflict."""
    docs = list(_values_by_doc(claims).values())
    return any(not (_covers(a, b) and _covers(b, a)) for i, a in enumerate(docs) for b in docs[i + 1:])


def resolve_rule(claims: list[Claim]) -> Resolution:
    if not claims:
        raise ValueError("no claims")
    rule = claims[0].rule
    survivors = list(claims)
    decided_by = None
    # Narrowing also runs when values agree, so the answer cites the strongest sources only.
    for name, key in (("authority", authority_rank), ("scope", scope_rank),
                      ("recency", lambda d: d.effective_date)):
        best = max(key(c.doc) for c in survivors)
        narrowed = [c for c in survivors if key(c.doc) == best]
        if decided_by is None and disagree(survivors) and not disagree(narrowed):
            decided_by = name
        survivors = narrowed
    losers = [c for c in claims if c not in survivors]
    return Resolution(rule=rule, winners=survivors, conflict=disagree(survivors),
                      decided_by=decided_by, losers=losers)


def group_by_rule(claims: list[Claim]) -> dict[str, list[Claim]]:
    groups: dict[str, list[Claim]] = {}
    for c in claims:
        groups.setdefault(c.rule, []).append(c)
    return groups


def resolve_all(current: list[Claim]) -> list[Resolution]:
    return [resolve_rule(cs) for _, cs in sorted(group_by_rule(current).items())]


def find_upcoming(current: list[Claim], future: list[Claim]) -> list[UpcomingChange]:
    """A future document is an upcoming change for a rule if, on its own effective date, it
    would win under the same precedence tests and state a value that differs from today's."""
    out: list[UpcomingChange] = []
    current_by_rule = group_by_rule(current)
    for rule, fclaims in sorted(group_by_rule(future).items()):
        base = current_by_rule.get(rule, [])
        current_values = resolve_rule(base).values if base else []
        for as_of in sorted({c.doc.effective_date for c in fclaims}):
            pool = base + [c for c in fclaims if c.doc.effective_date <= as_of]
            res = resolve_rule(pool)
            if res.conflict:
                continue
            for fc in res.winners:
                if fc.doc.effective_date == as_of and fc in fclaims \
                        and not any(same_value(fc.value, v) for v in current_values):
                    out.append(UpcomingChange(rule=rule, claim=fc, effective_date=as_of))
    return out
