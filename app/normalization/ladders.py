"""Named title ladders.

Our default seniority words are a *tech* ladder: junior, mid, senior, staff,
principal. Whole industries do not use it, and reading their titles with it is
not slightly wrong but inverted.

Banking is the clearest case. "Analyst" is the entry grade, yet it carries no
level word at all and reads as unknown. "Senior Analyst" is roughly three to
five years — a mid-level role — and the word "senior" makes it read two levels
higher than it is, so a reasonable application gets vetoed. "Assistant Vice
President" is four to seven years and reads as a director.

A ladder is an **industry convention**, not a company fact: the same grades run
across every bank, so one table serves all of them and a seventh bank costs a
single word in ``portals.txt``. That is the difference between this and a
per-board rule.

Mapped onto our own levels, deliberately generously at the bottom and strictly
at the top: the cost of reading an entry grade as mid is seeing one posting too
many, while the cost of reading a VP as mid is a feed full of jobs nobody at
two years will get.
"""

from __future__ import annotations

from app.models.enums import SeniorityLevel
from app.utils.text import normalize_key

#: Grades in each ladder, as ``(phrase, level)``.
#:
#: Order matters and is checked by a test: longer phrases must come first, or
#: "senior analyst" matches on "analyst" and loses the "senior".
_LADDERS: dict[str, tuple[tuple[str, SeniorityLevel], ...]] = {
    # Analyst -> Associate -> AVP -> VP -> ED -> MD. Used by essentially every
    # investment and corporate bank, with local variations in the middle.
    "banking": (
        ("managing director", SeniorityLevel.DIRECTOR),
        ("executive director", SeniorityLevel.DIRECTOR),
        ("assistant vice president", SeniorityLevel.SENIOR),
        ("associate vice president", SeniorityLevel.SENIOR),
        ("senior vice president", SeniorityLevel.DIRECTOR),
        ("vice president", SeniorityLevel.MANAGER),
        ("senior associate", SeniorityLevel.SENIOR),
        ("senior analyst", SeniorityLevel.MID),
        ("associate", SeniorityLevel.MID),
        ("analyst", SeniorityLevel.JUNIOR),
        # Abbreviations appear in titles as often as the spelled-out grade.
        ("md", SeniorityLevel.DIRECTOR),
        ("ed", SeniorityLevel.DIRECTOR),
        ("svp", SeniorityLevel.DIRECTOR),
        ("avp", SeniorityLevel.SENIOR),
        ("vp", SeniorityLevel.MANAGER),
    ),
}


def ladder_names() -> tuple[str, ...]:
    return tuple(sorted(_LADDERS))


def seniority_from_ladder(title: str | None, ladder: str | None) -> SeniorityLevel:
    """Read a grade out of ``title`` using a named ladder.

    Returns ``UNKNOWN`` when the ladder is unknown or the title carries no
    grade from it, so the caller falls back to the normal tech-ladder path —
    a bank's "Software Engineer III" is still read the usual way.
    """
    if not title or not ladder:
        return SeniorityLevel.UNKNOWN
    grades = _LADDERS.get(ladder.strip().lower())
    if not grades:
        return SeniorityLevel.UNKNOWN

    haystack = normalize_key(title)
    if not haystack:
        return SeniorityLevel.UNKNOWN
    words = haystack.split()
    for phrase, level in grades:
        target = normalize_key(phrase).split()
        if _contains_words(words, target):
            return level
    return SeniorityLevel.UNKNOWN


def _contains_words(words: list[str], target: list[str]) -> bool:
    """Whole-word subsequence match.

    Guards the abbreviations: a substring test would find "md" inside
    "Commodities" and "vp" inside nothing useful, turning a trading analyst
    into a managing director.
    """
    if not target:
        return False
    span = len(target)
    return any(words[i : i + span] == target for i in range(len(words) - span + 1))
