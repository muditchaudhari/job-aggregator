"""Employment type, seniority, and skill detection."""

from __future__ import annotations

import re

from app.models.enums import EmploymentType, SeniorityLevel
from app.utils.text import find_keywords, normalize_key

_EMPLOYMENT_PATTERNS: tuple[tuple[EmploymentType, tuple[str, ...]], ...] = (
    (EmploymentType.INTERNSHIP, ("intern", "internship", "industrial trainee", "co op", "coop")),
    (
        EmploymentType.CONTRACT,
        ("contract", "contractor", "freelance", "b2b", "consultant", "fixed term"),
    ),
    (EmploymentType.TEMPORARY, ("temporary", "temp", "seasonal", "casual")),
    (EmploymentType.PART_TIME, ("part time", "parttime", "part-time")),
    (EmploymentType.FULL_TIME, ("full time", "fulltime", "full-time", "permanent", "regular")),
)

#: Ordered most senior first. "Senior Staff Engineer" must resolve to staff,
#: not senior, so the more specific level has to be tested before the broader
#: one that is a substring of it.
_SENIORITY_PATTERNS: tuple[tuple[SeniorityLevel, tuple[str, ...]], ...] = (
    (SeniorityLevel.DIRECTOR, ("director", "vp", "vice president", "head of", "chief")),
    (SeniorityLevel.PRINCIPAL, ("principal", "distinguished", "fellow")),
    (SeniorityLevel.MANAGER, ("manager", "management", "supervisor")),
    # "staff" only counts as a *level*. "Member of Technical Staff" is a job
    # family used by Adobe, Oracle and others for early-career engineers, and
    # reading it as Staff-level inverted the seniority of an entire ladder.
    (SeniorityLevel.STAFF, ("staff", "senior staff")),
    (SeniorityLevel.LEAD, ("lead", "leader", "tech lead", "team lead")),
    (SeniorityLevel.SENIOR, ("senior", "sr", "snr", "experienced", "iii", "iv")),
    (SeniorityLevel.INTERN, ("intern", "internship", "trainee", "apprentice")),
    (SeniorityLevel.JUNIOR, ("junior", "jr", "graduate", "entry level", "associate", "i", "ii")),
)

#: Extracted from descriptions so the rule matcher can compute skill overlap
#: without re-scanning free text on every profile comparison.
KNOWN_SKILLS: tuple[str, ...] = (
    "python", "java", "javascript", "typescript", "go", "golang", "rust", "c++",
    "c#", "ruby", "php", "scala", "kotlin", "swift", "sql", "bash", "r",
    "react", "angular", "vue", "svelte", "next.js", "node.js", "express",
    "django", "flask", "fastapi", "spring", "spring boot", "spring mvc",
    "rails", "laravel", ".net", "graphql", "rest api", "grpc",
    "aws", "azure", "gcp", "lambda", "s3", "ec2", "dynamodb", "cloudformation",
    "terraform", "kubernetes", "docker", "jenkins", "ci/cd", "github actions",
    "ansible", "helm", "prometheus", "grafana", "datadog",
    "postgresql", "postgres", "mysql", "mongodb", "redis", "elasticsearch",
    "cassandra", "kafka", "rabbitmq", "snowflake", "bigquery", "spark",
    "hadoop", "airflow", "dbt", "etl",
    "machine learning", "deep learning", "nlp", "pytorch", "tensorflow",
    "scikit-learn", "pandas", "numpy", "llm", "computer vision",
    "microservices", "system design", "distributed systems", "agile", "scrum",
    "tdd", "unit testing", "linux", "git", "oauth", "kubernetes operators",
)

#: Two passes, because postings phrase this inconsistently and the two forms
#: carry different confidence.
#:
#: The strict form ("5+ years of relevant experience") is unambiguous. The
#: loose form ("3+ years of Python", "8 years") is what most requirement
#: bullets actually look like — demanding the literal word "experience", as an
#: earlier version did, matched almost nothing in practice.
_YEARS_STRICT_RE = re.compile(
    r"(?P<min>\d{1,2})\s*(?:\+|-|–|—|to)?\s*(?:\d{1,2})?\s*\+?\s*"
    r"(?:years?|yrs?)\s*(?:of\s+)?(?:[a-z]+\s+){0,3}?(?:experience|exp)\b",
    re.IGNORECASE,
)

_YEARS_LOOSE_RE = re.compile(
    r"(?P<min>\d{1,2})\s*(?:\+|-|–|—|to)?\s*(?:\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)

#: "Founded 10 years ago" is not a requirement. Cheap guard for the loose pass.
_AGO_RE = re.compile(r"^\W{0,3}(ago|old)\b", re.IGNORECASE)


def parse_employment_type(*values: str | None) -> EmploymentType:
    """Classify from any of title, explicit type field, or description."""
    haystack = normalize_key(" ".join(v for v in values if v))
    if not haystack:
        return EmploymentType.UNKNOWN
    for employment_type, patterns in _EMPLOYMENT_PATTERNS:
        for pattern in patterns:
            token = re.escape(normalize_key(pattern))
            if re.search(rf"(?<![a-z0-9]){token}(?![a-z0-9])", haystack):
                return employment_type
    return EmploymentType.UNKNOWN


#: Titles where the word "staff" is part of a job family, not a level.
_STAFF_FALSE_POSITIVES = ("member of technical staff", "technical staff", "staff nurse")

#: Numeric and roman level suffixes: "Engineer 4", "Engineer II", "SDE.5".
_LEVEL_RE = re.compile(
    r"(?:engineer|developer|scientist|analyst|designer|sde|mts|swe)\s*[.\-–]?\s*"
    r"(?P<level>\b[1-6]\b|\b(?:i{1,3}|iv|v)\b)(?![a-z0-9])",
    re.IGNORECASE,
)

_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5}

#: A company ladder position, mapped onto our levels. Deliberately conservative
#: at the top: a "4" is not universally Staff, but it is certainly not junior.
_LEVEL_TO_SENIORITY = {
    1: SeniorityLevel.JUNIOR,
    2: SeniorityLevel.MID,
    3: SeniorityLevel.SENIOR,
    4: SeniorityLevel.STAFF,
    5: SeniorityLevel.PRINCIPAL,
    6: SeniorityLevel.PRINCIPAL,
}

#: Nouns that make a preceding adjective a *role* level rather than prose.
#: Without this, "mentor junior talent" in a senior role's blurb reads as
#: "this is a junior role".
_ROLE_NOUNS = (
    "engineer", "developer", "scientist", "architect", "analyst", "designer",
    "manager", "programmer", "consultant", "specialist",
)

_ROLE_PHRASE_RE = re.compile(
    r"\b(?P<level>senior|sr|staff|principal|lead|junior|jr|associate|entry[- ]level)\b"
    r"[\s\w]{0,24}?\b(?:" + "|".join(_ROLE_NOUNS) + r")\b",
    re.IGNORECASE,
)

_PHRASE_TO_SENIORITY = {
    "senior": SeniorityLevel.SENIOR, "sr": SeniorityLevel.SENIOR,
    "staff": SeniorityLevel.STAFF, "principal": SeniorityLevel.PRINCIPAL,
    "lead": SeniorityLevel.LEAD, "junior": SeniorityLevel.JUNIOR,
    "jr": SeniorityLevel.JUNIOR, "associate": SeniorityLevel.JUNIOR,
    "entry-level": SeniorityLevel.JUNIOR, "entry level": SeniorityLevel.JUNIOR,
}


def parse_level_suffix(title: str | None) -> SeniorityLevel:
    """Read a numeric or roman ladder position out of a title.

    Adobe ships "Software Development Engineer 4" and "Member of Technical
    Staff II"; word-based matching sees no seniority in either and calls them
    unknown, which then scores as mid-level. The number *is* the level.
    """
    if not title:
        return SeniorityLevel.UNKNOWN
    match = _LEVEL_RE.search(title)
    if not match:
        return SeniorityLevel.UNKNOWN
    raw = match.group("level").lower()
    level = _ROMAN.get(raw) if raw in _ROMAN else int(raw)
    return _LEVEL_TO_SENIORITY.get(level or 0, SeniorityLevel.UNKNOWN)


def seniority_from_prose(text: str | None) -> SeniorityLevel:
    """Seniority stated in the body, e.g. "a Senior Backend Engineer".

    Requires the level word to sit just before a role noun. A blurb that says
    "mentor junior talent" is describing the team, not the vacancy.
    """
    if not text:
        return SeniorityLevel.UNKNOWN
    match = _ROLE_PHRASE_RE.search(text)
    if not match:
        return SeniorityLevel.UNKNOWN
    return _PHRASE_TO_SENIORITY.get(
        match.group("level").lower().replace("-", " "), SeniorityLevel.UNKNOWN
    )


def parse_seniority(title: str | None, description: str | None = None) -> SeniorityLevel:
    """Infer seniority, weighting the title far above the description.

    The title is where seniority is actually declared. A description mentioning
    "you will work with senior engineers" says nothing about the level of *this*
    role, so the description is consulted only when the title is silent, and
    then only for unambiguous words.
    """
    from_title = _match_seniority(normalize_key(title))
    if from_title is not SeniorityLevel.UNKNOWN:
        return from_title

    # "Engineer 4" / "MTS II" — the ladder position is the seniority.
    from_level = parse_level_suffix(title)
    if from_level is not SeniorityLevel.UNKNOWN:
        return from_level

    if description:
        # An explicit "Senior ... Engineer" in the body beats inferring from
        # a years figure, which many teasers omit entirely.
        from_prose = seniority_from_prose(description)
        if from_prose is not SeniorityLevel.UNKNOWN:
            return from_prose
        years = parse_years_required(description)
        if years is not None:
            return seniority_from_years(years)

    return SeniorityLevel.UNKNOWN


def _match_seniority(haystack: str) -> SeniorityLevel:
    if not haystack:
        return SeniorityLevel.UNKNOWN
    # Strip job-family uses of "staff" before level matching, so
    # "Member of Technical Staff II" is not read as a Staff engineer.
    for phrase in _STAFF_FALSE_POSITIVES:
        haystack = haystack.replace(normalize_key(phrase), " ")
    for level, patterns in _SENIORITY_PATTERNS:
        for pattern in patterns:
            if re.search(rf"(?<![a-z0-9]){re.escape(pattern)}(?![a-z0-9])", haystack):
                return level
    return SeniorityLevel.UNKNOWN


def parse_years_required(text: str | None) -> int | None:
    """Extract the minimum years of experience a posting asks for.

    Takes the *lowest* figure mentioned. Descriptions routinely list several
    ("3+ years of Python, 5+ years overall"), and the smallest is the one that
    governs whether it is worth applying.

    Explicit "years of experience" phrasings win outright when present; bare
    "N years" mentions are only consulted if there are none, which keeps a
    company's founding date out of the answer whenever a real requirement is
    stated.
    """
    if not text:
        return None

    strict = _collect_years(text, _YEARS_STRICT_RE, guard_ago=False)
    if strict:
        return min(strict)

    loose = _collect_years(text, _YEARS_LOOSE_RE, guard_ago=True)
    return min(loose) if loose else None


def _collect_years(text: str, pattern: re.Pattern[str], *, guard_ago: bool) -> list[int]:
    values: list[int] = []
    for match in pattern.finditer(text):
        raw = match.group("min")
        if not raw:
            continue
        years = int(raw)
        # Above ~40 it is a headcount, a revenue figure, or a year, not a
        # requirement anyone could meet.
        if years > 40:
            continue
        if guard_ago and _AGO_RE.match(text[match.end() : match.end() + 8]):
            continue
        values.append(years)
    return values


def seniority_from_years(years: int) -> SeniorityLevel:
    if years <= 0:
        return SeniorityLevel.INTERN
    if years <= 2:
        return SeniorityLevel.JUNIOR
    if years <= 5:
        return SeniorityLevel.MID
    if years <= 8:
        return SeniorityLevel.SENIOR
    return SeniorityLevel.STAFF


def detect_skills(*texts: str | None) -> list[str]:
    """Find known skills mentioned across title, description, requirements."""
    combined = " ".join(text for text in texts if text)
    if not combined:
        return []
    found = find_keywords(combined, list(KNOWN_SKILLS))
    # Preserve first-seen order while removing duplicates, so the stored list
    # reads naturally in a notification rather than alphabetically.
    seen: set[str] = set()
    unique: list[str] = []
    for skill in found:
        key = normalize_key(skill)
        if key in seen:
            continue
        seen.add(key)
        unique.append(skill)
    return unique
