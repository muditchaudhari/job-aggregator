from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from app.models.match import JobMatch
from app.repositories.base import BaseRepository


class JobMatchRepository(BaseRepository[JobMatch]):
    model = JobMatch

    def get_for_job(self, job_id: uuid.UUID, profile_id: uuid.UUID) -> JobMatch | None:
        stmt = sa.select(JobMatch).where(
            JobMatch.job_id == job_id, JobMatch.profile_id == profile_id
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def upsert(self, match: JobMatch) -> JobMatch:
        """Record a score, replacing any previous score for the same pair.

        Rescoring happens when the profile changes or the matcher is upgraded.
        Keeping one row per (job, profile) means "why was I notified" always
        has exactly one answer, and the unique constraint enforces it.
        """
        existing = self.get_for_job(match.job_id, match.profile_id)
        if existing is None:
            return self.add(match)

        existing.score = match.score
        existing.matched_skills = match.matched_skills
        existing.missing_skills = match.missing_skills
        existing.reasoning = match.reasoning
        existing.matcher = match.matcher
        existing.matcher_version = match.matcher_version
        existing.is_match = match.is_match
        self.session.flush()
        return existing

    def scores_for_jobs(
        self, job_ids: Sequence[uuid.UUID], profile_id: uuid.UUID
    ) -> dict[uuid.UUID, float]:
        """Bulk score lookup for the jobs listing endpoint."""
        if not job_ids:
            return {}
        rows = self.session.execute(
            sa.select(JobMatch.job_id, JobMatch.score).where(
                JobMatch.profile_id == profile_id,
                JobMatch.job_id.in_(list(job_ids)),
            )
        ).all()
        return {row[0]: float(row[1]) for row in rows}
