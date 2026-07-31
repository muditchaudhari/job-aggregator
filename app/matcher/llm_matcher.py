"""Semantic matching.

Adds the judgement rules cannot encode — whether a "Platform Engineer" posting
is really the backend role the candidate wants, whether "5 years in a
regulated environment" disqualifies them — and produces the human-readable
reasoning that goes into the notification.

Never runs on a whole board. The rule matcher shortlists first (AD-7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.errors import LLMError, LLMResponseError
from app.core.logging import get_logger
from app.llm.client import LLMClient, UsageTally, get_llm_client
from app.llm.prompts import MATCH_SYSTEM, MATCH_USER
from app.matcher.base import Matcher, MatchResult
from app.utils.text import clean_text, truncate

if TYPE_CHECKING:
    from app.models.job import Job
    from app.models.user import UserProfile

logger = get_logger(__name__)

#: Descriptions run to tens of thousands of characters and the fit signal is
#: overwhelmingly in the first part — responsibilities and requirements come
#: before the benefits and the EEO statement.
_DESCRIPTION_LIMIT = 6000

#: Generous relative to the ~150 tokens the answer needs, because reasoning
#: models bill their thinking against the same budget.
_MATCH_MAX_TOKENS = 2000


class LLMMatcher(Matcher):
    name = "llm"
    version = "1"

    def __init__(
        self, client: LLMClient | None = None, *, tally: UsageTally | None = None
    ) -> None:
        self._client = client
        self._tally = tally

    @property
    def client(self) -> LLMClient:
        if self._client is None:
            self._client = get_llm_client()
        return self._client

    def score(self, job: Job, profile: UserProfile) -> MatchResult:
        """Score semantically, falling back to the rule matcher on any failure.

        A model outage must degrade matching, not stop it. Returning the
        deterministic score keeps notifications flowing with slightly less
        nuance, which is strictly better than a silent gap in coverage.
        """
        try:
            response = self.client.complete(
                system=MATCH_SYSTEM,
                prompt=self._build_prompt(job, profile),
                purpose="job_matching",
                # The answer is ~150 tokens, but reasoning models spend output
                # budget thinking before they emit any of it. A tight cap gets
                # the JSON truncated mid-object — which surfaces as a parse
                # error and silently demotes every job to the rule matcher.
                max_tokens=_MATCH_MAX_TOKENS,
                tally=self._tally,
            )
            payload = response.json()
        except (LLMError, LLMResponseError) as exc:
            logger.warning("llm_matcher.failed", job=job.title, error=str(exc))
            return self._fallback(job, profile, reason=str(exc))

        if not isinstance(payload, dict):
            return self._fallback(job, profile, reason="response was not an object")

        return MatchResult(
            score=_as_score(payload.get("score")),
            matched_skills=_as_str_list(payload.get("matched_skills")),
            missing_skills=_as_str_list(payload.get("missing_skills")),
            reasoning=clean_text(payload.get("reasoning")) or None,
            matcher=self.name,
            matcher_version=self.version,
        )

    def _build_prompt(self, job: Job, profile: UserProfile) -> str:
        description = job.description or job.requirements or "(no description available)"
        return MATCH_USER.format(
            roles=", ".join(profile.preferred_roles or []) or "not specified",
            seniority=profile.seniority,
            years=profile.years_experience
            if profile.years_experience is not None
            else "not specified",
            skills=", ".join(profile.preferred_skills or []) or "not specified",
            locations=", ".join(profile.preferred_locations or []) or "any",
            remote_preference=profile.remote_preference,
            excluded=", ".join(profile.excluded_keywords or []) or "none",
            company=job.company.name if job.company else "unknown",
            title=job.title,
            location=job.location_raw or "not specified",
            remote_type=job.remote_type,
            employment_type=job.employment_type,
            description=truncate(description, _DESCRIPTION_LIMIT),
        )

    @staticmethod
    def _fallback(job: Job, profile: UserProfile, *, reason: str) -> MatchResult:
        from app.matcher.rule_matcher import RuleMatcher

        result = RuleMatcher().score(job, profile)
        result.reasoning = (
            f"{result.reasoning} (semantic scoring unavailable: {truncate(reason, 80)})"
        )
        return result


def _as_score(value: object) -> float:
    try:
        score = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    # Models sometimes answer on a 0-100 scale despite the prompt.
    if score > 1.0:
        score = score / 100 if score <= 100 else 1.0
    return round(max(0.0, min(1.0, score)), 4)


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [clean_text(item) for item in value if isinstance(item, str) and item.strip()]
