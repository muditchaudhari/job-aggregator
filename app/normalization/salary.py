"""Salary parsing.

Handles the formats that actually appear on job boards: ``$120,000 -
$150,000``, ``£45k–£60k per annum``, ``€80.000``, ``INR 25,00,000``,
``$60/hour``. Returns ``None`` rather than guessing when the string is
ambiguous — a wrong salary is worse than no salary, because it silently skews
every salary filter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.enums import SalaryPeriod
from app.utils.text import clean_text

_CURRENCY_SYMBOLS = {
    "$": "USD", "£": "GBP", "€": "EUR", "₹": "INR", "¥": "JPY",
    "₽": "RUB", "₩": "KRW", "C$": "CAD", "A$": "AUD", "R$": "BRL",
}
_CURRENCY_CODES = (
    "USD", "EUR", "GBP", "INR", "CAD", "AUD", "SGD", "CHF", "SEK", "NOK",
    "DKK", "PLN", "JPY", "CNY", "BRL", "MXN", "ZAR", "AED", "NZD", "ILS",
)

_PERIOD_PATTERNS = (
    (SalaryPeriod.HOUR, re.compile(r"\b(per\s+hour|/\s*(hr|hour)|hourly|an hour)\b", re.I)),
    (SalaryPeriod.DAY, re.compile(r"\b(per\s+day|/\s*day|daily|a day)\b", re.I)),
    (SalaryPeriod.WEEK, re.compile(r"\b(per\s+week|/\s*w(k|eek)|weekly)\b", re.I)),
    (SalaryPeriod.MONTH, re.compile(r"\b(per\s+month|/\s*mo(nth)?|monthly|a month|pm)\b", re.I)),
    (
        SalaryPeriod.YEAR,
        re.compile(r"\b(per\s+(annum|year)|/\s*(yr|year)|annual(ly)?|p\.?a\.?|a year)\b", re.I),
    ),
)

#: A number optionally followed by a k/m multiplier. Thousands separators may
#: be commas, dots, or spaces depending on locale, so all three are permitted
#: and resolved afterwards. The ``\d{2,3}`` group width also covers the Indian
#: convention (``25,00,000``).
#:
#: The separated branch requires *at least one* separator (``+``, not ``*``).
#: With ``*`` it matched the first three digits of an unseparated number and
#: won the alternation before ``\d+`` was ever tried — so a plain ``150000``
#: parsed as 150, which is exactly the kind of silent corruption a salary
#: filter cannot detect.
_NUMBER_RE = re.compile(
    r"(?P<number>\d{1,3}(?:[.,\s]\d{2,3})+|\d+(?:\.\d+)?)\s*(?P<suffix>[kKmM])?"
)

#: Below this, a yearly figure is implausible and the string was probably an
#: hourly rate, a headcount, or a year. Above ``_MAX_PLAUSIBLE`` it is a typo
#: or a non-salary number that happened to be in the field.
_MIN_PLAUSIBLE_ANNUAL = 1_000
_MAX_PLAUSIBLE = 100_000_000


@dataclass(slots=True)
class ParsedSalary:
    raw: str | None
    minimum: int | None = None
    maximum: int | None = None
    currency: str | None = None
    period: SalaryPeriod = SalaryPeriod.UNKNOWN

    @property
    def is_empty(self) -> bool:
        return self.minimum is None and self.maximum is None


def parse_salary(value: str | None) -> ParsedSalary:
    raw = clean_text(value)
    if not raw:
        return ParsedSalary(raw=None)

    currency = _detect_currency(raw)
    period = _detect_period(raw)
    amounts = _extract_amounts(raw)

    if not amounts:
        return ParsedSalary(raw=raw, currency=currency, period=period)

    minimum = amounts[0]
    maximum = amounts[1] if len(amounts) > 1 else None

    if maximum is not None and maximum < minimum:
        minimum, maximum = maximum, minimum

    # An unqualified figure in the tens of thousands is almost always annual;
    # saying so is more useful than leaving the period unknown, and it is the
    # inference every salary filter would otherwise have to make itself.
    if period is SalaryPeriod.UNKNOWN and minimum >= 10_000:
        period = SalaryPeriod.YEAR

    if period is SalaryPeriod.YEAR and minimum < _MIN_PLAUSIBLE_ANNUAL:
        return ParsedSalary(raw=raw, currency=currency, period=SalaryPeriod.UNKNOWN)

    return ParsedSalary(
        raw=raw,
        minimum=minimum,
        maximum=maximum,
        currency=currency,
        period=period,
    )


def _detect_currency(text: str) -> str | None:
    upper = text.upper()
    for code in _CURRENCY_CODES:
        if re.search(rf"\b{code}\b", upper):
            return code
    # Two-character symbols first, so "C$" is not read as a bare "$".
    for symbol in sorted(_CURRENCY_SYMBOLS, key=len, reverse=True):
        if symbol in text:
            return _CURRENCY_SYMBOLS[symbol]
    return None


def _detect_period(text: str) -> SalaryPeriod:
    for period, pattern in _PERIOD_PATTERNS:
        if pattern.search(text):
            return period
    return SalaryPeriod.UNKNOWN


def _extract_amounts(text: str) -> list[int]:
    amounts: list[int] = []
    for match in _NUMBER_RE.finditer(text):
        amount = _to_int(match.group("number"), match.group("suffix"))
        if amount is None:
            continue
        amounts.append(amount)
        if len(amounts) == 2:
            break
    return amounts


def _to_int(number: str, suffix: str | None) -> int | None:
    cleaned = number.strip()

    # Distinguishing "120,000" from "1,5" (European decimal comma) cannot be
    # done from the separator alone. The reliable signal is the length of the
    # final group: three digits means a thousands separator.
    if re.search(r"[.,\s]\d{3}(?:\D|$)", cleaned):
        cleaned = re.sub(r"[.,\s]", "", cleaned)
    else:
        cleaned = cleaned.replace(",", ".").replace(" ", "")

    try:
        value = float(cleaned)
    except ValueError:
        return None

    if suffix and suffix.lower() == "k":
        value *= 1_000
    elif suffix and suffix.lower() == "m":
        value *= 1_000_000

    if value <= 0 or value > _MAX_PLAUSIBLE:
        return None
    return round(value)


def to_annual(salary: ParsedSalary) -> int | None:
    """Convert the lower bound to an annual figure for comparison.

    Assumes 2 080 working hours, 260 days, and 52 weeks — the standard
    full-time-equivalent conversions. Used only for threshold comparisons,
    never stored, because the assumption does not hold for every posting.
    """
    if salary.minimum is None:
        return None
    multipliers = {
        SalaryPeriod.YEAR: 1,
        SalaryPeriod.MONTH: 12,
        SalaryPeriod.WEEK: 52,
        SalaryPeriod.DAY: 260,
        SalaryPeriod.HOUR: 2080,
    }
    multiplier = multipliers.get(salary.period)
    return salary.minimum * multiplier if multiplier else None
