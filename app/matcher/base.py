"""Matcher contract.

Two implementations today (rule, LLM) and an obvious third tomorrow
(embeddings). The interface exists so the scan pipeline depends on "something
that scores a job" rather than on any particular way of doing it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.job import Job
    from app.models.user import UserProfile


@dataclass(slots=True)
class MatchResult:
    score: float
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    reasoning: str | None = None
    matcher: str = "rule"
    matcher_version: str = "1"
    #: Set when a hard constraint disqualified the job outright. Distinct from
    #: a low score: a veto must never be overridden by a later, more
    #: enthusiastic matcher.
    vetoed: bool = False
    veto_reason: str | None = None

    def meets(self, threshold: float) -> bool:
        return not self.vetoed and self.score >= threshold


class Matcher(ABC):
    name: str = "base"
    version: str = "1"

    @abstractmethod
    def score(self, job: Job, profile: UserProfile) -> MatchResult:
        """Rate one posting against one profile."""
