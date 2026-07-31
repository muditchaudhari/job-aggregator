"""Matching orchestration.

Implements the two-stage policy from AD-7: rules on everything, the model on a
bounded shortlist. The bound is what keeps a 500-posting board from turning
into 500 model calls when a company first registers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import LLMBudgetExceededError
from app.core.logging import get_logger
from app.llm.client import LLMClient, UsageTally
from app.matcher.base import MatchResult
from app.matcher.llm_matcher import LLMMatcher
from app.matcher.rule_matcher import RuleMatcher
from app.models.match import JobMatch
from app.repositories.match import JobMatchRepository

if TYPE_CHECKING:
    from app.models.job import Job
    from app.models.user import UserProfile

logger = get_logger(__name__)

#: Jobs scoring below this after rules are not worth a model call — the model
#: is being asked to find nuance, not to overturn a clear mismatch. Set below
#: the notification threshold so borderline cases still get a second opinion.
_SEMANTIC_FLOOR_RATIO = 0.6


@dataclass(slots=True)
class ScoredJob:
    job: Job
    result: MatchResult

    @property
    def is_match(self) -> bool:
        return self.result.score > 0 and not self.result.vetoed


class MatchingService:
    def __init__(
        self,
        session: Session,
        *,
        llm_client: LLMClient | None = None,
        tally: UsageTally | None = None,
    ) -> None:
        self.session = session
        self.settings = get_settings()
        self.matches = JobMatchRepository(session)
        self.rule_matcher = RuleMatcher()
        self._llm_client = llm_client
        self._tally = tally

    def score_jobs(
        self,
        jobs: list[Job],
        profile: UserProfile,
        *,
        persist: bool = True,
    ) -> list[ScoredJob]:
        """Score a batch and return them ranked, best first."""
        threshold = profile.match_threshold or self.settings.match_threshold

        scored = [ScoredJob(job, self.rule_matcher.score(job, profile)) for job in jobs]

        if self.settings.match_semantic_enabled:
            self._apply_semantic(scored, profile, threshold)

        for entry in scored:
            entry.result.score = round(entry.result.score, 4)
            if persist:
                self._persist(entry, profile, threshold)

        scored.sort(key=lambda entry: entry.result.score, reverse=True)
        return scored

    def matches_above_threshold(
        self, scored: list[ScoredJob], profile: UserProfile
    ) -> list[ScoredJob]:
        threshold = profile.match_threshold or self.settings.match_threshold
        return [entry for entry in scored if entry.result.meets(threshold)]

    # -- Internals ---------------------------------------------------------

    def _apply_semantic(
        self, scored: list[ScoredJob], profile: UserProfile, threshold: float
    ) -> None:
        """Re-score the shortlist with the model, in place."""
        floor = threshold * _SEMANTIC_FLOOR_RATIO
        shortlist = [
            entry
            for entry in scored
            if not entry.result.vetoed and entry.result.score >= floor
        ]
        # Highest rule scores first, so that if the cap truncates the list, the
        # jobs that lose their second opinion are the least promising ones.
        shortlist.sort(key=lambda entry: entry.result.score, reverse=True)
        shortlist = shortlist[: self.settings.match_semantic_max_jobs_per_run]

        if not shortlist:
            return

        matcher = LLMMatcher(self._llm_client, tally=self._tally)
        if not matcher.client.is_available:
            logger.debug("matching.semantic_skipped", reason="llm unavailable")
            return

        logger.info(
            "matching.semantic",
            shortlisted=len(shortlist),
            of_total=len(scored),
        )

        for entry in shortlist:
            try:
                entry.result = matcher.score(entry.job, profile)
            except LLMBudgetExceededError as exc:
                # Budget is a global condition, not a per-job one; stop the
                # loop rather than failing the remaining jobs one at a time.
                logger.warning("matching.budget_exhausted", error=str(exc))
                break

    def _persist(self, entry: ScoredJob, profile: UserProfile, threshold: float) -> None:
        self.matches.upsert(
            JobMatch(
                job_id=entry.job.id,
                profile_id=profile.id,
                score=entry.result.score,
                matched_skills=entry.result.matched_skills,
                missing_skills=entry.result.missing_skills,
                reasoning=entry.result.reasoning,
                matcher=entry.result.matcher,
                matcher_version=entry.result.matcher_version,
                is_match=entry.result.meets(threshold),
            )
        )
