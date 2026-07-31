"""Deterministic matching.

Runs on every job, free and instantly. Two responsibilities:

* **Vetoes** — hard constraints that must never be overridden. An excluded
  keyword excludes, full stop; no semantic score gets a vote.
* **A cheap score** — good enough to rank, and good enough to decide which
  small subset is worth spending an LLM call on (AD-7).

Being permissive where evidence is missing is deliberate. A posting whose
location could not be parsed should not be silently dropped; boards write
locations no parser will ever handle, and dropping them means the user never
learns the job existed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.logging import get_logger
from app.matcher.base import Matcher, MatchResult
from app.models.enums import RemotePreference, RemoteType, SeniorityLevel
from app.normalization.fields import parse_years_required
from app.normalization.location import (
    ParsedLocation,
    has_geography,
    location_is_acceptable,
    matches_preferred,
    split_preferences,
)
from app.utils.text import contains_keyword, find_keywords, normalize_key

if TYPE_CHECKING:
    from app.models.job import Job
    from app.models.user import UserProfile

logger = get_logger(__name__)

#: Weights sum to 1.0. Skills are deliberately absent: they are *reported*
#: (so you can see the overlap at a glance) but they do not move the score.
#:
#: The reason is evidence quality. Whether a skill appears in a posting says
#: as much about how the company writes job ads as about the job — one board
#: publishes a full spec, the next a 300-character teaser, and scoring them on
#: the same scale ranks verbosity rather than fit. Title, seniority and
#: location are stated plainly and consistently nearly everywhere.
_WEIGHT_ROLE = 0.50
_WEIGHT_LOCATION = 0.25
_WEIGHT_SENIORITY = 0.25


def _parsed_location(job: Job) -> ParsedLocation:
    """Rebuild the parsed location from the columns normalisation wrote."""
    return ParsedLocation(
        raw=job.location_raw,
        city=job.location_city,
        region=job.location_region,
        country=job.location_country,
        remote_type=job.remote_type,
    )


def _as_seniority(value: object) -> SeniorityLevel | None:
    if isinstance(value, SeniorityLevel):
        return value
    if isinstance(value, str):
        try:
            return SeniorityLevel(value)
        except ValueError:
            return None
    return None


class RuleMatcher(Matcher):
    name = "rule"
    version = "1"

    def score(self, job: Job, profile: UserProfile) -> MatchResult:
        veto = self._veto(job, profile)
        if veto is not None:
            return MatchResult(
                score=0.0,
                matcher=self.name,
                matcher_version=self.version,
                vetoed=True,
                veto_reason=veto,
                reasoning=f"Excluded: {veto}",
            )

        role_score = self._role_score(job, profile)
        location_score = self._location_score(job, profile)
        seniority_score = self._seniority_score(job, profile)
        # Informational only — see the weights above.
        skills_matched, skills_missing = self._skills_seen(job, profile)

        total = (
            _WEIGHT_ROLE * role_score
            + _WEIGHT_LOCATION * location_score
            + _WEIGHT_SENIORITY * seniority_score
        )

        return MatchResult(
            score=round(min(1.0, max(0.0, total)), 4),
            matched_skills=skills_matched,
            missing_skills=skills_missing,
            reasoning=self._explain(
                role_score, location_score, seniority_score, skills_matched
            ),
            matcher=self.name,
            matcher_version=self.version,
        )

    # -- Hard constraints --------------------------------------------------

    def _veto(self, job: Job, profile: UserProfile) -> str | None:
        haystack = " ".join(
            part for part in (job.title, job.description, job.requirements) if part
        )
        for keyword in profile.excluded_keywords or []:
            if contains_keyword(haystack, keyword):
                return f"contains excluded keyword {keyword!r}"

        if profile.remote_preference is RemotePreference.REMOTE_ONLY:  # noqa: SIM102
            # UNKNOWN is not vetoed: most postings never state a remote policy,
            # and treating silence as "onsite" would discard nearly everything.
            if job.remote_type in (RemoteType.ONSITE, RemoteType.HYBRID):
                return f"role is {job.remote_type}, profile requires remote"

        # Experience is a filter, not a nudge. A Staff or Principal opening is
        # not a realistic application at two years however well the title and
        # location line up, and ranking it merely "lower" still puts it on the
        # page ahead of roles you could actually get.
        wanted = _as_seniority(profile.seniority)
        actual = _as_seniority(job.seniority)
        gap_limit = getattr(profile, "max_seniority_gap", 1)
        if (
            wanted is not None
            and actual is not None
            and wanted is not SeniorityLevel.UNKNOWN
            and actual is not SeniorityLevel.UNKNOWN
            and actual.rank - wanted.rank > gap_limit
        ):
            return (
                f"{actual} role is {actual.rank - wanted.rank} levels above "
                f"{wanted} (limit {gap_limit})"
            )

        strict_locations = (
            bool(profile.preferred_locations) and not profile.include_unknown_location
        )
        if strict_locations and not location_is_acceptable(
            _parsed_location(job),
            list(profile.preferred_locations),
            include_unknown=profile.include_unknown_location,
        ):
            return f"location {job.location_raw!r} is outside your preferred set"

        return None

    # -- Components --------------------------------------------------------

    @staticmethod
    def _role_score(job: Job, profile: UserProfile) -> float:
        roles = list(profile.preferred_roles or [])
        if not roles:
            return 0.5  # no stated preference: neutral, neither reward nor punish

        title = normalize_key(job.title)
        best = 0.0
        for role in roles:
            target = normalize_key(role)
            if not target:
                continue
            if target == title:
                return 1.0
            if target in title or title in target:
                best = max(best, 0.85)
                continue
            # Partial credit for token overlap: "senior backend engineer" vs
            # "backend software engineer" shares enough to be worth surfacing.
            role_tokens = set(target.split())
            title_tokens = set(title.split())
            if role_tokens and title_tokens:
                overlap = len(role_tokens & title_tokens) / len(role_tokens)
                best = max(best, overlap * 0.7)
        return best

    @staticmethod
    def _skills_seen(job: Job, profile: UserProfile) -> tuple[list[str], list[str]]:
        """Which of your skills the posting mentions. Reported, never scored."""
        wanted = list(profile.preferred_skills or [])
        if not wanted:
            return [], []

        haystack = " ".join(
            part
            for part in (
                job.title,
                job.description,
                job.requirements,
                " ".join(job.detected_skills or []),
            )
            if part
        )
        if not haystack.strip():
            return [], wanted

        matched = find_keywords(haystack, wanted)
        missing = [
            skill
            for skill in wanted
            if normalize_key(skill) not in {normalize_key(m) for m in matched}
        ]
        return matched, missing

    @staticmethod
    def _location_score(job: Job, profile: UserProfile) -> float:
        preferred = list(profile.preferred_locations or [])
        if not preferred:
            return 0.7

        parsed = _parsed_location(job)
        # Places only. Matching the bare word "Remote" against the job's text
        # would score a US-only remote role as a perfect location fit.
        places, wants_remote = split_preferences(preferred)
        if places and matches_preferred(parsed, places):
            return 1.0

        # Remote with no geography attached is open to anyone.
        if job.remote_type is RemoteType.REMOTE and not has_geography(parsed):
            return 1.0 if (wants_remote or not places) else 0.5

        # Remote, but tied to a country that is not one of yours. Scored low
        # rather than zero: these are occasionally still worth a look, and the
        # veto above already removes them when you asked for strict locations.
        if job.remote_type is RemoteType.REMOTE:
            return 0.25

        if not job.location_raw:
            return 0.5 if profile.include_unknown_location else 0.2
        return 0.1

    @staticmethod
    def _seniority_score(job: Job, profile: UserProfile) -> float:
        # Coerced rather than trusted. These are enum-typed columns, but a
        # value assigned from config (or by any caller passing a raw string)
        # stays a plain `str` until it round-trips through the database, and
        # `.rank` on a str is an AttributeError that fails the whole scan.
        wanted = _as_seniority(profile.seniority)
        actual = _as_seniority(job.seniority)
        if wanted is None or actual is None:
            return 0.5

        gap = actual.rank - wanted.rank
        if gap == 0:
            return 1.0
        if gap == 1:
            return 0.7  # a stretch role is worth seeing
        if gap == -1:
            return 0.5  # slightly junior; still plausible
        if gap > 1:
            return 0.15  # too senior to be a realistic application
        return 0.25  # significantly junior

    @staticmethod
    def _explain(
        role: float,
        location: float,
        seniority: float,
        matched: list[str],
    ) -> str:
        parts: list[str] = []
        if role >= 0.8:
            parts.append("title matches a preferred role")
        elif role >= 0.4:
            parts.append("title partially overlaps a preferred role")
        if matched:
            shown = ", ".join(matched[:5])
            parts.append(f"mentions {shown}")
        if location >= 0.9:
            parts.append("location works")
        elif location <= 0.2:
            parts.append("location is outside your preferences")
        if seniority >= 0.9:
            parts.append("seniority is a direct fit")
        elif seniority <= 0.2:
            parts.append("seniority is a poor fit")
        return "; ".join(parts) if parts else "no strong signals either way"


def years_gap(job: Job, profile: UserProfile) -> int | None:
    """How many years short of the posting's ask the candidate is.

    Not folded into the score — it is advisory, and postings state experience
    requirements too inconsistently to weight. Surfaced for the notification
    body and for the LLM matcher's context.
    """
    required = parse_years_required(job.description or job.requirements)
    if required is None or profile.years_experience is None:
        return None
    return max(0, required - profile.years_experience)
