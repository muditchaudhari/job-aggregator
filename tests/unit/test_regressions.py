"""Regressions for bugs that shipped once and must not return.

Each test names the failure it prevents rather than the function it calls —
these exist because something was silently wrong, not because a signature
needed covering.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.enums import (
    ATSType,
    EmploymentType,
    ExtractionTier,
    RemoteType,
    SalaryPeriod,
    SeniorityLevel,
)
from app.models.job import Job
from app.normalization.fields import parse_years_required
from app.normalization.salary import parse_salary
from app.scrapers.adapters.greenhouse import extract_board_token
from app.utils.text import strip_html


class TestEnumsSurviveTheDatabase:
    """Enum columns were plain ``String``, so anything *loaded* came back a str.

    Nothing caught it, because tests mostly assert against objects they just
    built in memory. In a worker — which always loads — ``profile.seniority``
    would be ``'mid'``, and ``.rank`` would raise ``AttributeError`` mid-scan.
    """

    def test_job_enums_load_as_enum_members(
        self, db_session: Session, company: Company
    ) -> None:
        db_session.add(
            Job(
                company_id=company.id,
                title="Backend Engineer",
                url="https://x.com/j1",
                content_hash=uuid.uuid4().hex,
                remote_type=RemoteType.HYBRID,
                employment_type=EmploymentType.FULL_TIME,
                seniority=SeniorityLevel.SENIOR,
                salary_period=SalaryPeriod.YEAR,
                extraction_tier=ExtractionTier.API,
            )
        )
        db_session.commit()
        # Drop the identity map so the row is genuinely re-read.
        db_session.expunge_all()

        job = db_session.query(Job).one()
        assert isinstance(job.remote_type, RemoteType)
        assert isinstance(job.employment_type, EmploymentType)
        assert isinstance(job.seniority, SeniorityLevel)
        assert isinstance(job.salary_period, SalaryPeriod)
        assert isinstance(job.extraction_tier, ExtractionTier)

    def test_seniority_rank_works_on_a_loaded_row(
        self, db_session: Session, profile: object
    ) -> None:
        """``.rank`` on a plain string is an AttributeError inside a worker."""
        from app.models.user import UserProfile

        db_session.commit()
        db_session.expunge_all()

        loaded = db_session.query(UserProfile).one()
        assert loaded.seniority.rank == SeniorityLevel.MID.rank

    def test_identity_comparison_holds_after_a_reload(
        self, db_session: Session
    ) -> None:
        """``company.ats_type is ATSType.GREENHOUSE`` must stay true."""
        db_session.add(
            Company(
                name="X",
                career_url="https://boards.greenhouse.io/x",
                website="greenhouse.io",
                ats_type=ATSType.GREENHOUSE,
            )
        )
        db_session.commit()
        db_session.expunge_all()

        loaded = db_session.query(Company).one()
        assert loaded.ats_type is ATSType.GREENHOUSE


class TestTasksBindToTheConfiguredBroker:
    """``.delay()`` from the API process published to nowhere.

    ``@shared_task`` binds to the *current* Celery app. Workers get ours from
    `-A`; the API process did not, so it fell back to Celery's default app
    (broker ``None`` → ``amqp://localhost``) and every enqueue was refused.
    Invisible in a worker-only test, and invisible in production too, because
    enqueue failures are non-fatal by design.
    """

    def test_tasks_use_the_application_broker(self) -> None:
        from app.core.config import get_settings
        from app.scheduler.tasks import detect_company, scan_company

        expected = get_settings().celery_broker_url
        for task in (detect_company, scan_company):
            assert task.app.conf.broker_url == expected, (
                f"{task.name} is bound to {task.app.main!r}, not the configured app"
            )


class TestDoublyEscapedHtml:
    """Greenhouse returns HTML that has itself been HTML-escaped.

    Stripping tags before decoding entities saw no tags at all, and the later
    unescape resurrected them as literal text — so every Greenhouse
    description reached the user with ``<strong>`` in it.
    """

    def test_escaped_markup_is_fully_stripped(self) -> None:
        raw = "&lt;p&gt;We use &lt;strong&gt;Python&lt;/strong&gt; and AWS.&lt;/p&gt;"
        result = strip_html(raw)
        assert "<strong>" not in result
        assert "&lt;" not in result
        assert result == "We use Python and AWS."

    def test_plain_markup_still_works(self) -> None:
        assert strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_block_boundaries_become_line_breaks(self) -> None:
        result = strip_html("<li>First</li><li>Second</li>")
        assert result.splitlines() == ["First", "Second"]

    def test_entities_that_are_not_markup_survive(self) -> None:
        assert strip_html("Ben &amp; Jerry&rsquo;s") == "Ben & Jerry’s"


class TestUnseparatedSalaries:
    """``150000`` parsed as ``150``.

    The separated branch of the number pattern used ``*`` rather than ``+``, so
    it matched the first three digits and won the alternation before the plain
    ``\\d+`` branch was ever tried. Silent, and invisible to any salary filter.
    """

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("USD 150000", 150_000),
            ("150000 - 200000 per year", 150_000),
            ("$95000/year", 95_000),
            ("120,000", 120_000),
            ("1250000", 1_250_000),
        ],
    )
    def test_unseparated_numbers_keep_their_magnitude(
        self, text: str, expected: int
    ) -> None:
        assert parse_salary(text).minimum == expected


class TestGreenhouseEmbedToken:
    """``embed`` was extracted as the board slug.

    The bare-board pattern was tried first and happily matched the literal
    ``embed`` path segment, so embedded boards resolved to a board token that
    does not exist — and every scrape 404'd.
    """

    @pytest.mark.parametrize(
        "source",
        [
            "https://boards.greenhouse.io/embed/job_board?for=acme",
            "https://boards.greenhouse.io/embed/job_board/js?for=acme",
            '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>',
        ],
    )
    def test_embed_forms_yield_the_real_token(self, source: str) -> None:
        assert extract_board_token(source) == "acme"

    def test_bare_board_url_still_works(self) -> None:
        assert extract_board_token("https://boards.greenhouse.io/acme") == "acme"


class TestBareYearsRequirements:
    """Requiring the literal word "experience" matched almost nothing.

    Real bullets say "3+ years of Python", not "3+ years of experience", so the
    parser returned ``None`` for most postings and seniority inference silently
    stopped working.
    """

    def test_bare_years_are_found(self) -> None:
        assert parse_years_required("3+ years of Python") == 3

    def test_lowest_requirement_wins(self) -> None:
        text = "5+ years overall, 3+ years of Python, 8 years preferred"
        assert parse_years_required(text) == 3

    def test_explicit_experience_phrasing_beats_incidental_numbers(self) -> None:
        """A company's age must not become its experience requirement."""
        text = "Founded 12 years ago. You have 4+ years of experience."
        assert parse_years_required(text) == 4

    def test_ago_is_ignored_when_nothing_else_is_stated(self) -> None:
        assert parse_years_required("We were founded 12 years ago.") is None


class TestSkillsAreReportedNotScored:
    """Skills must never move the score — only title, experience, location do.

    They used to carry 40% of the weight, which meant the ranking largely
    reflected how verbosely each board writes its ads: a company publishing a
    full spec beat one publishing a 300-character teaser for the same role.
    Skills are still surfaced so the overlap is visible at a glance.
    """

    def _job(self, company_id, title: str, description: str):
        import uuid as _uuid

        from app.models.job import Job

        return Job(
            company_id=company_id,
            title=title,
            url=f"https://x.com/{_uuid.uuid4().hex[:8]}",
            location_raw="Bangalore, India",
            location_city="Bangalore",
            description=description,
            content_hash=_uuid.uuid4().hex,
        )

    def test_identical_roles_score_the_same_whatever_the_skill_overlap(
        self, company, profile
    ) -> None:
        from app.matcher.rule_matcher import RuleMatcher

        matcher = RuleMatcher()
        rich = self._job(
            company.id, "Backend Engineer", "Python, Java, AWS, Docker, SQL, React."
        )
        bare = self._job(company.id, "Backend Engineer", "Cobol and Fortran only.")

        assert matcher.score(rich, profile).score == matcher.score(bare, profile).score

    def test_a_teaser_is_not_penalised_against_a_full_spec(
        self, company, profile
    ) -> None:
        from app.matcher.rule_matcher import RuleMatcher

        matcher = RuleMatcher()
        teaser = self._job(company.id, "Backend Engineer", "Join our team.")
        full = self._job(
            company.id, "Backend Engineer", "Python and AWS. " + ("Detail. " * 200)
        )
        assert matcher.score(teaser, profile).score == matcher.score(full, profile).score

    def test_skills_are_still_reported(self, company, profile) -> None:
        from app.matcher.rule_matcher import RuleMatcher

        job = self._job(company.id, "Backend Engineer", "We use Python and AWS daily.")
        result = RuleMatcher().score(job, profile)
        assert "Python" in result.matched_skills
        assert "AWS" in result.matched_skills
        assert result.missing_skills, "unmatched profile skills should be listed"


class TestSeniorityIsReadCorrectly:
    """Three ways an over-senior role slipped through as a match.

    Adobe's "Software Development Engineer 4 - Fullstack - Backend heavy"
    wants 8+ years and scored 0.85 for a two-year candidate.
    """

    def test_numeric_ladder_position_is_a_level(self) -> None:
        """"Engineer 4" carried no seniority word, so it parsed as unknown —
        which then scored as mid-level."""
        from app.normalization.fields import parse_seniority

        assert parse_seniority("Software Development Engineer 4 - Fullstack") is (
            SeniorityLevel.STAFF
        )
        assert parse_seniority("Software Development Engineer.5") is (
            SeniorityLevel.PRINCIPAL
        )

    def test_member_of_technical_staff_is_not_staff_level(self) -> None:
        """A job family, not a rung. Reading it as Staff inverted a whole ladder."""
        from app.normalization.fields import parse_seniority

        assert parse_seniority("Member of Technical Staff II") is SeniorityLevel.JUNIOR
        assert parse_seniority("Staff Backend Engineer") is SeniorityLevel.STAFF

    def test_seniority_stated_in_the_body_is_used(self) -> None:
        """The teaser said "Senior Software Development Engineer" and we
        ignored it, because only years-of-experience was consulted."""
        from app.normalization.fields import parse_seniority

        teaser = (
            "Join our team as a Senior Software Development Engineer, Fullstack, "
            "and lead the design of backend features. Mentor junior talent."
        )
        assert parse_seniority("Software Development Engineer", teaser) is (
            SeniorityLevel.SENIOR
        )

    def test_prose_about_the_team_is_not_the_role_level(self) -> None:
        from app.normalization.fields import seniority_from_prose

        assert seniority_from_prose("You will mentor junior talent daily.") is (
            SeniorityLevel.UNKNOWN
        )

    def test_over_senior_roles_are_vetoed_not_merely_ranked_down(
        self, company, profile
    ) -> None:
        from app.matcher.rule_matcher import RuleMatcher
        from app.models.job import Job

        profile.seniority = SeniorityLevel.JUNIOR
        profile.max_seniority_gap = 1
        job = Job(
            company_id=company.id,
            title="Software Development Engineer 4 - Fullstack",
            url="https://x.com/j",
            location_raw="Bangalore, India",
            location_city="Bangalore",
            seniority=SeniorityLevel.STAFF,
            content_hash=uuid.uuid4().hex,
        )
        result = RuleMatcher().score(job, profile)
        assert result.vetoed
        assert result.score == 0.0
        assert "levels above" in (result.veto_reason or "")

    def test_a_one_level_stretch_is_still_allowed(self, company, profile) -> None:
        from app.matcher.rule_matcher import RuleMatcher
        from app.models.job import Job

        profile.seniority = SeniorityLevel.JUNIOR
        profile.max_seniority_gap = 1
        job = Job(
            company_id=company.id,
            title="Backend Engineer",
            url="https://x.com/j2",
            location_raw="Bangalore, India",
            location_city="Bangalore",
            seniority=SeniorityLevel.MID,
            content_hash=uuid.uuid4().hex,
        )
        assert not RuleMatcher().score(job, profile).vetoed


class TestInsertedJobsStayEditable:
    """``insert_new`` returned transient objects on PostgreSQL.

    The bulk path uses a Core ``INSERT``, which attaches nothing to the
    session. Callers then mutated the returned objects — detail enrichment
    filling in a description, a corrected seniority — and every edit was
    discarded at commit. Matching used the in-memory values, so scores and
    stored rows disagreed and nothing raised.
    """

    def test_returned_jobs_are_attached_to_the_session(
        self, db_session: Session, company: Company
    ) -> None:
        from app.repositories.job import JobRepository

        repo = JobRepository(db_session)
        fresh = repo.insert_new(
            [
                Job(
                    company_id=company.id,
                    title="Backend Engineer",
                    url="https://x.com/j1",
                    content_hash=uuid.uuid4().hex,
                )
            ]
        )
        assert len(fresh) == 1
        assert fresh[0] in db_session, "returned job must be session-attached"

        fresh[0].description = "Requires 8+ years of experience."
        db_session.commit()
        db_session.expunge_all()

        reloaded = db_session.query(Job).one()
        assert reloaded.description == "Requires 8+ years of experience."


class TestRobotsLongestMatchWins:
    """``urllib.robotparser`` resolves conflicts by file order, not specificity.

    The blanket-disallow-plus-specific-allow pattern is everywhere — Microsoft's
    careers site publishes exactly it — and reading it in file order refuses a
    path we are explicitly invited to fetch. RFC 9309: longest pattern wins,
    Allow breaks ties.
    """

    MICROSOFT = """
    User-agent: *
    Disallow: /
    Allow: /$
    Allow: /careers
    Allow: /api/apply
    """

    def test_specific_allow_beats_blanket_disallow(self) -> None:
        from app.scrapers.robots import RobotsFile

        robots = RobotsFile(self.MICROSOFT)
        assert robots.can_fetch("/careers?start=0&location=Bengaluru")
        assert robots.can_fetch("/api/apply/v2/jobs")
        assert not robots.can_fetch("/candidate/profile")

    def test_root_anchor_is_honoured(self) -> None:
        from app.scrapers.robots import RobotsFile

        robots = RobotsFile(self.MICROSOFT)
        assert robots.can_fetch("/")
        assert not robots.can_fetch("/something-else")

    def test_empty_disallow_permits_everything(self) -> None:
        from app.scrapers.robots import RobotsFile

        assert RobotsFile("User-agent: *\nDisallow:").can_fetch("/anything")

    def test_wildcards_and_end_anchor(self) -> None:
        from app.scrapers.robots import RobotsFile

        robots = RobotsFile("User-agent: *\nDisallow: /*.pdf$\nAllow: /")
        assert not robots.can_fetch("/files/report.pdf")
        assert robots.can_fetch("/files/report.pdf?download=1")
        assert robots.can_fetch("/files/report.html")

    def test_named_agent_group_replaces_the_wildcard_group(self) -> None:
        from app.scrapers.robots import RobotsFile

        robots = RobotsFile(
            "User-agent: *\nDisallow: /\n\nUser-agent: examplebot\nAllow: /\n"
        )
        assert robots.can_fetch("/jobs", "examplebot/1.0")
        assert not robots.can_fetch("/jobs", "Mozilla/5.0")

    def test_a_plain_disallow_still_blocks(self) -> None:
        from app.scrapers.robots import RobotsFile

        robots = RobotsFile("User-agent: *\nDisallow: /private\nAllow: /")
        assert not robots.can_fetch("/private/data")
        assert robots.can_fetch("/public/data")


class TestTrailingSlashIsPreserved:
    """Canonicalisation stripped trailing slashes, which changes the resource.

    ``jobs.uber.com/en/jobs/`` returns 200; ``/en/jobs`` returns 403. The
    slash is part of the path, not tracking noise, and removing it made a
    working portal unfetchable.
    """

    def test_trailing_slash_survives(self) -> None:
        from app.utils.urls import canonicalize_url

        assert canonicalize_url("https://jobs.uber.com/en/jobs/?location=X") == (
            "https://jobs.uber.com/en/jobs/?location=X"
        )

    def test_absent_slash_is_not_added(self) -> None:
        from app.utils.urls import canonicalize_url

        assert canonicalize_url("https://x.com/jobs/5") == "https://x.com/jobs/5"

    def test_tracking_parameters_are_still_stripped(self) -> None:
        from app.utils.urls import canonicalize_url

        assert canonicalize_url("https://x.com/jobs/?utm_source=li&id=7") == (
            "https://x.com/jobs/?id=7"
        )


class TestRedislessFallbacks:
    """A cron runner has no Redis, and both Redis-backed helpers failed badly.

    The rate limiter failed *open*, letting every request through unthrottled
    from a datacentre address — the environment where boards are least
    forgiving. The budget tracker failed *closed*, reporting the breaker open
    and disabling the LLM tier permanently rather than for an outage.
    """

    @staticmethod
    def _offline_limiter(rpm: int):
        import redis

        from app.scrapers.rate_limit import RateLimiter

        class Unreachable:
            def register_script(self, _src):
                def _raise(*_a, **_k):
                    raise redis.ConnectionError("Connection refused")

                return _raise

        return RateLimiter(client=Unreachable(), requests_per_minute=rpm)  # type: ignore[arg-type]

    def test_limiter_still_throttles_without_redis(self) -> None:
        limiter = self._offline_limiter(rpm=2)

        assert limiter.try_acquire("example.com")[0] is True
        assert limiter.try_acquire("example.com")[0] is True
        allowed, wait = limiter.try_acquire("example.com")
        assert allowed is False
        assert wait > 0

    def test_limiter_buckets_are_per_domain(self) -> None:
        limiter = self._offline_limiter(rpm=1)

        assert limiter.try_acquire("a.com")[0] is True
        assert limiter.try_acquire("a.com")[0] is False
        # A different board must not inherit the first one's exhausted bucket.
        assert limiter.try_acquire("b.com")[0] is True

    def test_budget_does_not_report_the_breaker_open_without_redis(self) -> None:
        import redis

        from app.llm.budget import BudgetTracker

        class Unreachable:
            def get(self, *_a, **_k):
                raise redis.ConnectionError("Connection refused")

        tracker = BudgetTracker(client=Unreachable())  # type: ignore[arg-type]
        status = tracker.status()

        assert status.breaker_open is False
        assert status.exhausted is False

    def test_budget_still_counts_spend_in_process(self) -> None:
        import redis

        from app.llm.budget import BudgetTracker

        class Unreachable:
            def get(self, *_a, **_k):
                raise redis.ConnectionError("Connection refused")

            def pipeline(self):
                raise redis.ConnectionError("Connection refused")

        tracker = BudgetTracker(client=Unreachable())  # type: ignore[arg-type]
        tracker.record(0.25)
        tracker.record(0.25)

        assert tracker.status().spent_usd == 0.5
