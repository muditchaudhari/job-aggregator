"""Matching: rule scoring, vetoes, and the semantic shortlist policy."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.matcher.rule_matcher import RuleMatcher
from app.matcher.service import MatchingService
from app.models.enums import EmploymentType, RemotePreference, RemoteType, SeniorityLevel
from app.models.job import Job


def make_job(company_id: uuid.UUID, **overrides: Any) -> Job:
    defaults: dict[str, Any] = {
        "company_id": company_id,
        "title": "Backend Engineer",
        "url": "https://x.com/jobs/1",
        "location_raw": "Bengaluru, India",
        "location_city": "Bengaluru",
        "location_country": "India",
        "remote_type": RemoteType.ONSITE,
        "employment_type": EmploymentType.FULL_TIME,
        "seniority": SeniorityLevel.MID,
        "description": "Build services in Python on AWS with Docker and SQL.",
        "detected_skills": ["python", "aws", "docker", "sql"],
        "content_hash": uuid.uuid4().hex,
    }
    defaults.update(overrides)
    return Job(**defaults)


class TestRuleMatcher:
    def test_strong_match_scores_high(self, company: Any, profile: Any) -> None:
        job = make_job(company.id)
        result = RuleMatcher().score(job, profile)
        assert result.score >= 0.7
        assert not result.vetoed
        assert "python" in [skill.lower() for skill in result.matched_skills]

    def test_excluded_keyword_vetoes_outright(self, company: Any, profile: Any) -> None:
        """A veto must never be overridable by a high score elsewhere."""
        job = make_job(company.id, title="Enterprise Sales Engineer")
        result = RuleMatcher().score(job, profile)
        assert result.vetoed
        assert result.score == 0.0
        assert "Sales" in (result.veto_reason or "")

    def test_excluded_keyword_respects_word_boundaries(
        self, company: Any, profile: Any
    ) -> None:
        profile.excluded_keywords = ["lead"]
        job = make_job(company.id, description="Strong leadership skills valued.")
        assert RuleMatcher().score(job, profile).vetoed is False

    def test_remote_only_profile_vetoes_onsite(self, company: Any, profile: Any) -> None:
        profile.remote_preference = RemotePreference.REMOTE_ONLY
        job = make_job(company.id, remote_type=RemoteType.ONSITE)
        assert RuleMatcher().score(job, profile).vetoed

    def test_remote_only_profile_tolerates_unknown(
        self, company: Any, profile: Any
    ) -> None:
        """Most postings never state a policy; treating silence as onsite
        would discard nearly everything."""
        profile.remote_preference = RemotePreference.REMOTE_ONLY
        job = make_job(company.id, remote_type=RemoteType.UNKNOWN)
        assert not RuleMatcher().score(job, profile).vetoed

    def test_overly_senior_role_scores_low(self, company: Any, profile: Any) -> None:
        job = make_job(
            company.id, title="Director of Engineering", seniority=SeniorityLevel.DIRECTOR
        )
        senior = RuleMatcher().score(job, profile)
        peer = RuleMatcher().score(make_job(company.id), profile)
        assert senior.score < peer.score

    def test_missing_skills_are_reported(self, company: Any, profile: Any) -> None:
        job = make_job(
            company.id, description="PHP and Laravel shop.", detected_skills=["php"]
        )
        result = RuleMatcher().score(job, profile)
        assert "Python" in result.missing_skills

    def test_listing_only_job_is_not_punished_for_having_no_description(
        self, company: Any, profile: Any
    ) -> None:
        """No description is a property of the board, not of the job's fit."""
        job = make_job(company.id, description=None, detected_skills=[], title="Engineer")
        assert RuleMatcher().score(job, profile).score > 0.0

    def test_empty_profile_stays_neutral(self, company: Any, profile: Any) -> None:
        profile.preferred_roles = []
        profile.preferred_skills = []
        profile.preferred_locations = []
        profile.excluded_keywords = []
        result = RuleMatcher().score(make_job(company.id), profile)
        assert 0.3 <= result.score <= 0.8

    def test_reasoning_is_always_populated(self, company: Any, profile: Any) -> None:
        assert RuleMatcher().score(make_job(company.id), profile).reasoning


class TestMatchingService:
    def test_persists_one_row_per_job_and_profile(
        self, db_session: Session, company: Any, profile: Any
    ) -> None:
        jobs = [make_job(company.id, title=f"Backend Engineer {i}") for i in range(3)]
        db_session.add_all(jobs)
        db_session.flush()

        scored = MatchingService(db_session).score_jobs(jobs, profile)

        assert len(scored) == 3
        from app.repositories.match import JobMatchRepository

        repository = JobMatchRepository(db_session)
        assert all(repository.get_for_job(job.id, profile.id) is not None for job in jobs)

    def test_rescoring_updates_rather_than_duplicating(
        self, db_session: Session, company: Any, profile: Any
    ) -> None:
        job = make_job(company.id)
        db_session.add(job)
        db_session.flush()

        service = MatchingService(db_session)
        service.score_jobs([job], profile)
        service.score_jobs([job], profile)

        from app.models.match import JobMatch

        count = (
            db_session.query(JobMatch)
            .filter(JobMatch.job_id == job.id, JobMatch.profile_id == profile.id)
            .count()
        )
        assert count == 1

    def test_results_are_ranked_best_first(
        self, db_session: Session, company: Any, profile: Any
    ) -> None:
        good = make_job(company.id, title="Backend Engineer")
        poor = make_job(
            company.id,
            title="Warehouse Operative",
            description="Lifting and packing.",
            detected_skills=[],
            location_raw="Leeds, United Kingdom",
            location_city="Leeds",
        )
        db_session.add_all([good, poor])
        db_session.flush()

        scored = MatchingService(db_session).score_jobs([poor, good], profile)
        assert scored[0].job.title == "Backend Engineer"

    def test_threshold_filters(
        self, db_session: Session, company: Any, profile: Any
    ) -> None:
        profile.match_threshold = 0.99
        # Not an exact title match: with skills removed from the score, an
        # exact title in the right place at the right level scores a true 1.0.
        job = make_job(company.id, title="Backend Engineer, Payments")
        db_session.add(job)
        db_session.flush()

        service = MatchingService(db_session)
        scored = service.score_jobs([job], profile)
        assert service.matches_above_threshold(scored, profile) == []

    @pytest.mark.parametrize("threshold", [0.0, 0.5, 1.0])
    def test_threshold_boundaries_do_not_error(
        self, db_session: Session, company: Any, profile: Any, threshold: float
    ) -> None:
        profile.match_threshold = threshold
        job = make_job(company.id)
        db_session.add(job)
        db_session.flush()

        service = MatchingService(db_session)
        service.matches_above_threshold(service.score_jobs([job], profile), profile)
