"""Normalisation: dates, locations, salaries, derived fields, hashing."""

from __future__ import annotations

from datetime import date

import pytest

from app.models.enums import (
    EmploymentType,
    RemoteType,
    SalaryPeriod,
    SeniorityLevel,
)
from app.normalization.dates import parse_posted_date
from app.normalization.fields import (
    detect_skills,
    parse_employment_type,
    parse_seniority,
    parse_years_required,
)
from app.normalization.hashing import compute_content_hash
from app.normalization.location import parse_location
from app.normalization.salary import parse_salary, to_annual

TODAY = date(2026, 7, 30)


class TestDates:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("Today", TODAY),
            ("Just posted", TODAY),
            ("Yesterday", date(2026, 7, 29)),
            ("3 days ago", date(2026, 7, 27)),
            ("Posted 3 Days Ago", date(2026, 7, 27)),
            ("2 weeks ago", date(2026, 7, 16)),
            ("1 month ago", date(2026, 6, 30)),
            ("2026-07-22", date(2026, 7, 22)),
            ("2026-07-20T10:30:00-04:00", date(2026, 7, 20)),
            ("July 28", date(2026, 7, 28)),
            ("28 July 2026", date(2026, 7, 28)),
        ],
    )
    def test_parses_common_formats(self, value: str, expected: date) -> None:
        assert parse_posted_date(value, today=TODAY) == expected

    def test_epoch_milliseconds(self) -> None:
        # Lever reports milliseconds; 1753000000000 is 2025-07-20.
        assert parse_posted_date("1753000000000", today=TODAY) == date(2025, 7, 20)

    def test_short_numbers_are_not_timestamps(self) -> None:
        """A job id must not be read as an epoch and become 1970-01-01."""
        assert parse_posted_date("12345", today=TODAY) is None

    def test_bare_month_day_rolls_back_rather_than_going_future(self) -> None:
        """"December 15" seen in July is last December, not five months ahead."""
        result = parse_posted_date("December 15", today=TODAY)
        assert result == date(2025, 12, 15)

    def test_relative_bucket_resolves_to_today(self) -> None:
        assert parse_posted_date("Posted within the last 30 days", today=TODAY) == TODAY

    @pytest.mark.parametrize("value", [None, "", "   ", "sometime soon", "ASAP"])
    def test_returns_none_rather_than_guessing(self, value: str | None) -> None:
        assert parse_posted_date(value, today=TODAY) is None


class TestLocation:
    def test_city_region_country(self) -> None:
        parsed = parse_location("Bengaluru, Karnataka, India")
        assert parsed.city == "Bengaluru"
        assert parsed.region == "Karnataka"
        assert parsed.country == "India"
        assert parsed.remote_type is RemoteType.UNKNOWN

    def test_us_state_abbreviation(self) -> None:
        parsed = parse_location("San Francisco, CA, USA")
        assert parsed.city == "San Francisco"
        assert parsed.country == "United States"

    def test_remote_detected_from_text(self) -> None:
        assert parse_location("Remote - US").remote_type is RemoteType.REMOTE

    def test_hybrid_beats_remote(self) -> None:
        """"Hybrid Remote" is hybrid; checking remote first would get it wrong."""
        assert parse_location("Hybrid Remote, London").remote_type is RemoteType.HYBRID

    def test_structured_hint_wins(self) -> None:
        parsed = parse_location("London", hint="remote")
        assert parsed.remote_type is RemoteType.REMOTE

    def test_placeholder_yields_no_city(self) -> None:
        parsed = parse_location("Multiple Locations")
        assert parsed.city is None
        assert parsed.raw == "Multiple Locations"

    def test_parenthetical_is_kept(self) -> None:
        parsed = parse_location("Remote (India)")
        assert parsed.remote_type is RemoteType.REMOTE
        assert parsed.country == "India"

    def test_acronyms_are_not_mangled(self) -> None:
        assert parse_location("NYC, NY").city == "NYC"


class TestSalary:
    def test_range_with_symbol(self) -> None:
        parsed = parse_salary("$120,000 - $150,000 per year")
        assert (parsed.minimum, parsed.maximum) == (120_000, 150_000)
        assert parsed.currency == "USD"
        assert parsed.period is SalaryPeriod.YEAR

    def test_k_suffix(self) -> None:
        parsed = parse_salary("£45k–£60k per annum")
        assert (parsed.minimum, parsed.maximum) == (45_000, 60_000)
        assert parsed.currency == "GBP"

    def test_hourly(self) -> None:
        parsed = parse_salary("$60/hour")
        assert parsed.minimum == 60
        assert parsed.period is SalaryPeriod.HOUR

    def test_european_thousands_separator(self) -> None:
        parsed = parse_salary("€80.000 per year")
        assert parsed.minimum == 80_000

    def test_reversed_range_is_corrected(self) -> None:
        parsed = parse_salary("USD 150000 to 120000")
        assert (parsed.minimum, parsed.maximum) == (120_000, 150_000)

    def test_unparseable_returns_empty(self) -> None:
        assert parse_salary("Competitive").is_empty

    def test_annualisation(self) -> None:
        assert to_annual(parse_salary("$60/hour")) == 60 * 2080


class TestFields:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Software Engineer Intern", EmploymentType.INTERNSHIP),
            ("Backend Developer (Contract)", EmploymentType.CONTRACT),
            ("Part-time Analyst", EmploymentType.PART_TIME),
            ("Full time Engineer", EmploymentType.FULL_TIME),
            ("Engineer", EmploymentType.UNKNOWN),
        ],
    )
    def test_employment_type(self, text: str, expected: EmploymentType) -> None:
        assert parse_employment_type(text) is expected

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Senior Software Engineer", SeniorityLevel.SENIOR),
            ("Staff Engineer", SeniorityLevel.STAFF),
            ("Principal Architect", SeniorityLevel.PRINCIPAL),
            ("Engineering Manager", SeniorityLevel.MANAGER),
            ("Director of Engineering", SeniorityLevel.DIRECTOR),
            ("Junior Developer", SeniorityLevel.JUNIOR),
            ("Software Engineer", SeniorityLevel.UNKNOWN),
        ],
    )
    def test_seniority_from_title(self, title: str, expected: SeniorityLevel) -> None:
        assert parse_seniority(title) is expected

    def test_seniority_falls_back_to_years(self) -> None:
        assert parse_seniority("Engineer", "We want 7+ years of experience") is (
            SeniorityLevel.SENIOR
        )

    def test_years_takes_the_lowest_requirement(self) -> None:
        text = "5+ years overall, 3+ years of Python, 8 years preferred"
        assert parse_years_required(text) == 3

    def test_skill_detection_is_word_bounded(self) -> None:
        """"Go" must not match inside "Django"."""
        skills = detect_skills("Django and React developer")
        assert "django" in skills
        assert "go" not in skills

    def test_skills_deduplicated(self) -> None:
        skills = detect_skills("Python, python, PYTHON and AWS")
        assert skills.count("python") == 1


class TestHashing:
    def test_is_deterministic(self) -> None:
        args = {
            "company_id": "c1",
            "title": "Backend Engineer",
            "location": "Bengaluru",
            "url": "https://x.com/jobs/1",
        }
        assert compute_content_hash(**args) == compute_content_hash(**args)

    def test_ignores_tracking_parameters(self) -> None:
        """URL churn is the main cause of false 'new job' notifications."""
        base = {"company_id": "c1", "title": "Backend Engineer", "location": "Pune"}
        first = compute_content_hash(**base, url="https://x.com/jobs/1?utm_source=li")
        second = compute_content_hash(**base, url="https://x.com/jobs/1?gh_src=abc")
        assert first == second

    def test_ignores_title_whitespace_and_case(self) -> None:
        base = {"company_id": "c1", "location": "Pune", "url": "https://x.com/1"}
        assert compute_content_hash(**base, title="Backend  Engineer") == (
            compute_content_hash(**base, title="backend engineer")
        )

    def test_external_id_dominates(self) -> None:
        """A requisition id survives retitling and relocation."""
        first = compute_content_hash(
            company_id="c1", title="Backend Engineer", location="Pune", external_id="R-1"
        )
        second = compute_content_hash(
            company_id="c1", title="Senior Backend Engineer", location="Remote",
            external_id="R-1",
        )
        assert first == second

    def test_different_companies_never_collide(self) -> None:
        args = {"title": "Backend Engineer", "location": "Pune", "url": "https://x.com/1"}
        assert compute_content_hash(company_id="c1", **args) != compute_content_hash(
            company_id="c2", **args
        )
