"""Checks for AI-written text: a denylist, no links, mentions
or markup, and no bettor names. A failing text drops that proposal or rewrite only."""

import re
from collections.abc import Iterable
from functools import cache
from pathlib import Path

DENYLIST_FILE = Path(__file__).with_name("denylist.txt")
_BLOCKED = re.compile(
    r"https?://|www\.|discord\.gg|[@<>`*_|\[\]]|\b[\w.-]+\.(?:com|net|org|io|gg|ly)\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-z0-9']+")


@cache
def denylist() -> frozenset[str]:
    lines = DENYLIST_FILE.read_text(encoding="utf-8").splitlines()
    return frozenset(w.strip().lower() for w in lines if w.strip() and not w.startswith("#"))


def problem(text: str, names: Iterable[str] = ()) -> str | None:
    """Why this text can't be shown, or None. `names` are bettor display names."""
    if not text.strip():
        return "empty_text"
    if any(ord(c) < 32 and c not in "\n" for c in text):
        return "control_chars"
    if _BLOCKED.search(text):
        return "link_or_markup"
    lowered = text.lower()
    words = set(_WORD.findall(lowered))
    if words & denylist():
        return "denylist"
    for name in names:
        clean = name.strip().lower()
        if len(clean) >= 2 and re.search(rf"(?<![a-z0-9]){re.escape(clean)}(?![a-z0-9])", lowered):
            return "bettor_name"
    return None
