"""Extraction: URL handling, selectors, embedded JSON, reduction, scoring."""

from __future__ import annotations

import pytest

from app.extractors.embedded_json import extract_embedded_jobs
from app.extractors.html_reducer import reduce_html
from app.extractors.selector_extractor import SelectorSet, extract_with_selectors
from app.extractors.validation import score_extraction
from app.models.enums import SelectorStrategy
from app.scrapers.base import RawJob
from app.utils.text import contains_keyword, find_keywords, normalize_key
from app.utils.urls import canonicalize_url, registrable_domain
from tests.fixtures.pages import (
    GENERIC_LISTING_HTML,
    JSON_LD_HTML,
    NO_JOBS_HTML,
    SPA_SHELL_HTML,
)

BASE_URL = "https://widgets.example.com/careers"


class TestUrls:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://boards.greenhouse.io/acme", "greenhouse.io"),
            ("https://www.example.com/careers", "example.com"),
            ("https://careers.acme.co.uk/jobs", "acme.co.uk"),
            ("https://jobs.eu.lever.co/acme", "lever.co"),
        ],
    )
    def test_registrable_domain(self, url: str, expected: str) -> None:
        assert registrable_domain(url) == expected

    def test_canonicalize_strips_tracking_and_fragment(self) -> None:
        """The trailing slash stays — it is part of the path, not noise."""
        messy = "https://X.com/jobs/1/?utm_source=li&gh_src=a&id=7#apply"
        assert canonicalize_url(messy) == "https://x.com/jobs/1/?id=7"

    def test_canonicalize_sorts_query_parameters(self) -> None:
        assert canonicalize_url("https://x.com/j?b=2&a=1") == canonicalize_url(
            "https://x.com/j?a=1&b=2"
        )

    def test_canonicalize_resolves_relative(self) -> None:
        assert (
            canonicalize_url("/jobs/5", base="https://x.com/careers")
            == "https://x.com/jobs/5"
        )


class TestText:
    def test_keyword_matching_respects_word_boundaries(self) -> None:
        assert contains_keyword("Leadership skills", "lead") is False
        assert contains_keyword("Team Lead wanted", "lead") is True

    def test_multi_word_keyword(self) -> None:
        assert contains_keyword("We use machine learning", "machine learning") is True

    def test_find_keywords_returns_originals(self) -> None:
        found = find_keywords("Python and AWS", ["python", "AWS", "Rust"])
        assert found == ["python", "AWS"]

    def test_normalize_key_folds_punctuation_and_accents(self) -> None:
        assert normalize_key("Sr. Software Engineer (m/f/d)") == (
            "sr software engineer m f d"
        )


class TestSelectorExtraction:
    def test_extracts_container_scoped_fields(self) -> None:
        selectors = SelectorSet(
            container="li.job-item",
            title="a",
            url="a",
            location="span.job-location",
            date="time.job-date",
        )
        jobs = extract_with_selectors(GENERIC_LISTING_HTML, BASE_URL, selectors)

        assert len(jobs) == 3
        assert jobs[0].title == "Backend Engineer"
        assert jobs[0].url == "https://widgets.example.com/careers/jobs/backend-engineer"
        assert jobs[0].location == "Bengaluru, India"

    def test_self_pseudo_selector(self) -> None:
        """A bare <a> per posting: title, link, and container are one node."""
        selectors = SelectorSet(container="li.job-item a", title="self", url="self")
        jobs = extract_with_selectors(GENERIC_LISTING_HTML, BASE_URL, selectors)
        assert [job.title for job in jobs] == [
            "Backend Engineer",
            "Frontend Engineer",
            "DevOps Engineer",
        ]

    def test_attribute_fallback_when_element_has_no_text(self) -> None:
        html = (
            '<ul><li class="j"><a href="/1">Role</a>'
            '<time datetime="2026-07-01"></time></li></ul>'
        )
        selectors = SelectorSet(container="li.j", title="a", url="a", date="time")
        jobs = extract_with_selectors(html, BASE_URL, selectors)
        assert jobs[0].posted_at == "2026-07-01"

    def test_malformed_selector_returns_empty_not_crash(self) -> None:
        selectors = SelectorSet(container="li[[[broken", title="a")
        assert extract_with_selectors(GENERIC_LISTING_HTML, BASE_URL, selectors) == []

    def test_unusable_selector_set_short_circuits(self) -> None:
        assert extract_with_selectors(GENERIC_LISTING_HTML, BASE_URL, SelectorSet()) == []

    def test_xpath_strategy(self) -> None:
        selectors = SelectorSet(
            container="//li[@class='job-item']",
            title=".//a",
            url=".//a/@href",
            location=".//span[@class='job-location']",
            strategy=SelectorStrategy.XPATH,
        )
        jobs = extract_with_selectors(GENERIC_LISTING_HTML, BASE_URL, selectors)
        assert len(jobs) == 3
        assert jobs[1].location == "Remote"


class TestEmbeddedJson:
    def test_json_ld_job_posting(self) -> None:
        jobs = extract_embedded_jobs(JSON_LD_HTML, BASE_URL)
        assert len(jobs) == 1
        job = jobs[0]
        assert job.title == "Machine Learning Engineer"
        assert job.external_id == "ML-77"
        assert job.location == "Bengaluru, Karnataka, India"
        assert job.salary is not None and "2500000" in job.salary

    def test_next_data_state(self) -> None:
        jobs = extract_embedded_jobs(SPA_SHELL_HTML, BASE_URL)
        assert {job.title for job in jobs} == {"Cloud Engineer", "Security Engineer"}
        assert jobs[0].url.startswith("https://widgets.example.com")

    def test_page_without_json_yields_nothing(self) -> None:
        assert extract_embedded_jobs(GENERIC_LISTING_HTML, BASE_URL) == []

    def test_prefers_the_largest_candidate_list(self) -> None:
        """Pages ship both a short 'featured' array and the real board."""
        html = """<script id="__NEXT_DATA__" type="application/json">
        {"props":{"featured":{"jobs":[{"title":"Solo","url":"/1"}]},
        "page":{"jobs":[{"title":"A","url":"/a"},{"title":"B","url":"/b"},
        {"title":"C","url":"/c"}]}}}</script>"""
        jobs = extract_embedded_jobs(html, BASE_URL)
        assert len(jobs) == 3


class TestHtmlReducer:
    def test_finds_the_listing_and_shrinks_the_page(self) -> None:
        reduced = reduce_html(GENERIC_LISTING_HTML, max_chars=8000)
        assert "Backend Engineer" in reduced.html
        assert reduced.candidate_count == 3
        assert reduced.reduced_bytes < reduced.original_bytes

    def test_drops_scripts_and_chrome(self) -> None:
        html = GENERIC_LISTING_HTML.replace(
            "</body>", "<script>var tracking='x';</script></body>"
        )
        reduced = reduce_html(html)
        assert "tracking" not in reduced.html
        assert "Privacy policy" not in reduced.html

    def test_caps_repeated_items(self) -> None:
        rows = "".join(
            f'<li class="job-item"><a href="/jobs/{i}">Engineer {i}</a></li>'
            for i in range(40)
        )
        reduced = reduce_html(f'<ul class="job-list">{rows}</ul>', max_items=5)
        assert reduced.html.count("job-item") <= 6

    def test_respects_the_character_budget(self) -> None:
        rows = "".join(
            f'<li class="job-item"><a href="/jobs/{i}">Engineer {i}</a>'
            f'<span class="job-location">City {i}</span></li>'
            for i in range(500)
        )
        reduced = reduce_html(f'<ul class="job-list">{rows}</ul>', max_chars=2000)
        assert reduced.reduced_bytes <= 2000


class TestValidation:
    def _jobs(self, count: int, **overrides: object) -> list[RawJob]:
        return [
            RawJob(
                title=overrides.get("title") or f"Software Engineer {i}",
                url=overrides.get("url") or f"https://x.com/jobs/{i}",
                location=overrides.get("location", "Bengaluru"),
                posted_at=overrides.get("posted_at", "2026-07-28"),
            )
            for i in range(count)
        ]

    def test_good_extraction_scores_high(self) -> None:
        score = score_extraction(self._jobs(5))
        assert score.confidence >= 0.8
        assert score.is_acceptable

    def test_empty_extraction_scores_zero(self) -> None:
        score = score_extraction([])
        assert score.confidence == 0.0
        assert not score.is_acceptable

    def test_identical_titles_are_penalised(self) -> None:
        """The fingerprint of a selector that latched onto a template."""
        jobs = [
            RawJob(title="Apply now", url=f"https://x.com/{i}") for i in range(6)
        ]
        score = score_extraction(jobs)
        assert not score.is_acceptable
        assert any("identical" in reason for reason in score.reasons)

    def test_navigation_links_are_rejected(self) -> None:
        jobs = [
            RawJob(title="Home", url="https://x.com/home"),
            RawJob(title="About", url="https://x.com/about"),
            RawJob(title="Cookie policy", url="https://x.com/cookies"),
            RawJob(title="Sign in", url="https://x.com/login"),
        ]
        assert not score_extraction(jobs).is_acceptable

    def test_single_shared_url_is_penalised(self) -> None:
        jobs = [
            RawJob(title=f"Engineer {i}", url="https://x.com/careers") for i in range(4)
        ]
        assert not score_extraction(jobs).is_acceptable

    def test_missing_fields_are_reported(self) -> None:
        jobs = [RawJob(title=f"Engineer {i}", url=f"https://x.com/{i}") for i in range(4)]
        score = score_extraction(jobs)
        assert score.missing_fields["location"] == 1.0
        assert "url" not in score.missing_fields

    def test_page_with_no_openings_scores_zero(self) -> None:
        selectors = SelectorSet(container="li.job-item", title="a", url="a")
        jobs = extract_with_selectors(NO_JOBS_HTML, BASE_URL, selectors)
        assert not score_extraction(jobs).is_acceptable
