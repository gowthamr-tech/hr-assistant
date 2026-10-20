"""Deterministic checks that run before (and independently of) the LLM interpreter."""
from __future__ import annotations

import re
from datetime import date, timedelta

_MANIPULATION = [
    r"\bignore (all |any |the |your |previous |prior |above |earlier )*(instructions|rules|prompts?|guidelines)",
    r"\bdisregard (all |any |the |your |previous |prior )*(instructions|rules|prompts?)",
    r"\b(system|developer|hidden|initial|original) (prompt|instructions|message)",
    r"\b(reveal|show|print|repeat|dump|leak|output) (me )?(your|the) (prompt|instructions|rules|configuration|config)",
    r"\bjailbreak|\bdeveloper mode\b|\bdan mode\b|\bgod mode\b",
    r"\byou are now\b|\bpretend (to be|you are)\b|\bact as (an? )?(admin|administrator|hr|root|system)",
    r"\b(bypass|override|disable) (the |your |all )?(security|permissions?|access control|restrictions|rules|checks)",
]
_BULK = [
    r"\b(everyone|everybody|all (the )?(employees|staff|people|users|managers|team members)|every employee|"
    r"whole (company|team|org)|entire (company|team|org)|each employee)'?s?\b.*\b(balances?|leave requests?|"
    r"emails?|phones?|phone numbers?|contact|salar(y|ies)|details|records)\b",
    r"\b(balances?|leave requests?|emails?|phone numbers?|contact details|salar(y|ies))\b.*\b(of|for) "
    r"(everyone|everybody|all (the )?(employees|staff|people|managers|team members)|every employee|each employee)\b",
    r"\b(list|show|give|export|dump)( me)? (all|every) (employees|staff|people|users|employee records)\b",
]
_PERSONAL = [
    r"\b(phone|mobile|cell|telephone) (number|no\.?)\b",
    r"\bhome address\b",
]

_CONFIRM = re.compile(
    r"^(yes|y|yeah|yep|yup|sure|ok|okay|confirm|confirmed|i confirm|go ahead|go for it|do it|please do|proceed|"
    r"submit( it)?|book it|yes please|please proceed|sounds good|correct|that'?s (right|correct)|"
    r"approved?|absolutely|of course|definitely|affirmative)"
    r"([ ,.!]+(yes|please|go ahead|do it|confirm|proceed|thanks|thank you|submit it|that'?s fine|sure))*[ .!]*$",
    re.IGNORECASE,
)
_REJECT = re.compile(
    r"^(no|n|nope|nah|don'?t|do not|stop|never ?mind|forget it|cancel that|abort|not now|no thanks|"
    r"no,? thank you|leave it|don'?t do it|do not do it|no,? don'?t)[ .!]*$",
    re.IGNORECASE,
)
_LEAVE_TYPE_WORDS = {
    "annual": re.compile(r"\b(annual|vacation|holiday leave|pto|paid time off|earned leave|privilege leave)\b", re.I),
    "sick": re.compile(r"\b(sick|ill|illness|unwell|medical|doctor)\b", re.I),
}


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def is_manipulation(text: str) -> bool:
    return _any(_MANIPULATION, text)


def is_bulk_request(text: str) -> bool:
    return _any(_BULK, text)


def is_personal_data_request(text: str) -> bool:
    return _any(_PERSONAL, text)


def is_confirmation(text: str) -> bool:
    return bool(_CONFIRM.match(text.strip()))


def is_rejection(text: str) -> bool:
    """Only a bare "no". "No, make it until Friday" changes the details and goes to the interpreter."""
    return bool(_REJECT.match(text.strip()))


def leave_type_stated(texts: list[str], leave_type: str) -> bool:
    """The leave type must come from the user's own words; it is never assumed."""
    rx = _LEAVE_TYPE_WORDS.get(leave_type)
    return bool(rx) and any(rx.search(t or "") for t in texts)


# ------------------------------------------------------------------ date context for the interpreter
def date_context(today: date) -> str:
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    this_monday = today - timedelta(days=today.weekday())
    next_monday = this_monday + timedelta(days=7)
    lines = [
        f"today: {today.isoformat()} ({names[today.weekday()]})",
        f"tomorrow: {(today + timedelta(days=1)).isoformat()}",
        f"this week (Mon-Fri): {this_monday.isoformat()} to {(this_monday + timedelta(days=4)).isoformat()}",
        f"next week (Mon-Fri): {next_monday.isoformat()} to {(next_monday + timedelta(days=4)).isoformat()}",
    ]
    for i, name in enumerate(names):
        delta = (i - today.weekday()) % 7 or 7
        lines.append(f"next {name}: {(today + timedelta(days=delta)).isoformat()}")
    days = [f"{(today + timedelta(days=k)).isoformat()} {names[(today + timedelta(days=k)).weekday()][:3]}"
            for k in range(0, 43)]
    lines.append("next 6 weeks: " + ", ".join(days))
    return "\n".join(lines)
