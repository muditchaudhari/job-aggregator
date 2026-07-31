"""API tests.

The app is built against the in-memory database by overriding ``get_db``, and
the Celery ``.delay`` calls are stubbed — an endpoint's contract is "did it
accept the request and enqueue the right work", not "did the broker run it".
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, ClassVar

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import transactional
from app.database.session import get_db
from app.main import create_app


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Capture Celery dispatches instead of requiring a live broker."""
    calls: list[tuple[str, dict[str, Any]]] = []

    class _Stub:
        def __init__(self, name: str) -> None:
            self.name = name

        def delay(self, *args: Any, **kwargs: Any) -> None:
            calls.append((self.name, {"args": args, "kwargs": kwargs}))

    import app.scheduler.tasks as tasks

    monkeypatch.setattr(tasks, "scan_company", _Stub("scan_company"))
    monkeypatch.setattr(tasks, "detect_company", _Stub("detect_company"))
    return calls


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    def _override_transactional() -> Iterator[Session]:
        yield db_session
        db_session.flush()

    app.dependency_overrides[get_db] = _override
    app.dependency_overrides[transactional] = _override_transactional

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


class TestSystem:
    def test_health_reports_each_dependency(self, client: TestClient) -> None:
        response = client.get("/health")
        # Redis is not running in unit tests, so degraded is the correct answer.
        assert response.status_code in (200, 503)
        body = response.json()
        assert set(body["checks"]) == {"database", "redis"}

    def test_metrics_is_prometheus_exposition(self, client: TestClient) -> None:
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        assert "jap_pages_scraped_total" in response.text

    def test_openapi_is_served(self, client: TestClient) -> None:
        assert client.get("/openapi.json").status_code == 200


class TestCompanies:
    def test_create_returns_201_and_queues_detection(
        self, client: TestClient, enqueued: list[Any]
    ) -> None:
        response = client.post(
            "/api/v1/companies",
            json={"career_url": "https://boards.greenhouse.io/acme", "name": "Acme"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Acme"
        assert body["website"] == "greenhouse.io"
        # Registration must not block on a network round trip.
        assert any(name == "detect_company" for name, _ in enqueued)

    def test_name_is_inferred_from_an_ats_url(
        self, client: TestClient, enqueued: list[Any]
    ) -> None:
        response = client.post(
            "/api/v1/companies", json={"career_url": "https://jobs.lever.co/widget-co"}
        )
        assert response.json()["name"] == "Widget Co"

    def test_duplicate_url_returns_409(
        self, client: TestClient, enqueued: list[Any]
    ) -> None:
        payload = {"career_url": "https://boards.greenhouse.io/acme"}
        assert client.post("/api/v1/companies", json=payload).status_code == 201
        assert client.post("/api/v1/companies", json=payload).status_code == 409

    def test_invalid_url_returns_422(self, client: TestClient) -> None:
        assert client.post("/api/v1/companies", json={"career_url": "nope"}).status_code == 422

    def test_list_and_filter(self, client: TestClient, company: Any) -> None:
        response = client.get("/api/v1/companies", params={"is_active": True})
        assert response.status_code == 200
        assert response.json()["total"] >= 1

    def test_detail_includes_job_counts(self, client: TestClient, company: Any) -> None:
        response = client.get(f"/api/v1/companies/{company.id}")
        assert response.status_code == 200
        body = response.json()
        assert body["total_jobs"] == 0
        assert body["ats_type"] == "greenhouse"

    def test_detail_404(self, client: TestClient) -> None:
        import uuid

        assert client.get(f"/api/v1/companies/{uuid.uuid4()}").status_code == 404

    def test_delete_is_a_soft_delete(self, client: TestClient, company: Any) -> None:
        assert client.delete(f"/api/v1/companies/{company.id}").status_code == 200
        # Still retrievable — its jobs and notification history survive.
        assert client.get(f"/api/v1/companies/{company.id}").status_code == 200
        assert client.get(f"/api/v1/companies/{company.id}").json()["is_active"] is False


class TestProfile:
    PAYLOAD: ClassVar[dict[str, Any]] = {
        "email": "me@example.com",
        "full_name": "Test User",
        "preferred_roles": ["Software Engineer"],
        "preferred_skills": ["Python", "AWS"],
        "preferred_locations": ["Bangalore"],
        "excluded_keywords": ["Sales"],
        "seniority": "mid",
        "remote_preference": "any",
        "years_experience": 3,
        "match_threshold": 0.65,
    }

    def test_create(self, client: TestClient) -> None:
        response = client.post("/api/v1/profile", json=self.PAYLOAD)
        assert response.status_code == 201
        assert response.json()["preferred_skills"] == ["Python", "AWS"]

    def test_create_is_idempotent_by_email(self, client: TestClient) -> None:
        client.post("/api/v1/profile", json=self.PAYLOAD)
        second = client.post("/api/v1/profile", json={**self.PAYLOAD, "seniority": "senior"})
        assert second.status_code == 201
        assert second.json()["seniority"] == "senior"

    def test_update_is_partial(self, client: TestClient) -> None:
        client.post("/api/v1/profile", json=self.PAYLOAD)
        response = client.put("/api/v1/profile", json={"match_threshold": 0.8})
        assert response.status_code == 200
        body = response.json()
        assert body["match_threshold"] == 0.8
        # Untouched fields survive.
        assert body["preferred_skills"] == ["Python", "AWS"]

    def test_update_can_clear_a_list(self, client: TestClient) -> None:
        """``exclude_unset`` not ``exclude_none`` — clearing is a real update."""
        client.post("/api/v1/profile", json=self.PAYLOAD)
        response = client.put("/api/v1/profile", json={"excluded_keywords": []})
        assert response.json()["excluded_keywords"] == []

    def test_update_without_a_profile_returns_404(self, client: TestClient) -> None:
        assert client.put("/api/v1/profile", json={"match_threshold": 0.7}).status_code == 404

    def test_threshold_out_of_range_returns_422(self, client: TestClient) -> None:
        response = client.post("/api/v1/profile", json={**self.PAYLOAD, "match_threshold": 5})
        assert response.status_code == 422


class TestJobs:
    def _add_job(self, db_session: Session, company: Any, title: str) -> None:
        import uuid

        from app.models.job import Job

        db_session.add(
            Job(
                company_id=company.id,
                title=title,
                url=f"https://x.com/{uuid.uuid4().hex[:8]}",
                location_raw="Bengaluru, India",
                location_city="Bengaluru",
                content_hash=uuid.uuid4().hex,
            )
        )
        db_session.flush()

    def test_list_is_paginated(
        self, client: TestClient, db_session: Session, company: Any
    ) -> None:
        for index in range(5):
            self._add_job(db_session, company, f"Engineer {index}")

        response = client.get("/api/v1/jobs", params={"limit": 2})
        body = response.json()
        assert len(body["items"]) == 2
        assert body["total"] == 5

    def test_filter_by_title(
        self, client: TestClient, db_session: Session, company: Any
    ) -> None:
        self._add_job(db_session, company, "Backend Engineer")
        self._add_job(db_session, company, "Product Designer")

        response = client.get("/api/v1/jobs", params={"title": "backend"})
        assert response.json()["total"] == 1

    def test_filter_by_location(
        self, client: TestClient, db_session: Session, company: Any
    ) -> None:
        self._add_job(db_session, company, "Backend Engineer")
        assert client.get("/api/v1/jobs", params={"location": "bengaluru"}).json()["total"] == 1

    def test_new_jobs_window(
        self, client: TestClient, db_session: Session, company: Any
    ) -> None:
        from datetime import timedelta

        from app.models.job import Job
        from app.utils.time import utcnow

        self._add_job(db_session, company, "Fresh Engineer")
        db_session.execute(
            sa.update(Job)
            .where(Job.title == "Fresh Engineer")
            .values(first_seen_at=utcnow() - timedelta(days=10))
        )
        db_session.flush()

        assert client.get("/api/v1/jobs/new", params={"hours": 24}).json()["total"] == 0
        assert client.get("/api/v1/jobs/new", params={"hours": 720}).json()["total"] == 1

    def test_job_detail_404(self, client: TestClient) -> None:
        import uuid

        assert client.get(f"/api/v1/jobs/{uuid.uuid4()}").status_code == 404


class TestScans:
    def test_scan_returns_202_and_enqueues(
        self, client: TestClient, company: Any, enqueued: list[Any], db_session: Session
    ) -> None:
        company.next_scrape_at = None
        # The test session has autoflush off (matching production), so the
        # pending change must be flushed before the endpoint queries for it.
        db_session.flush()

        response = client.post("/api/v1/scan", json={})
        assert response.status_code == 202
        assert response.json()["scheduled"] >= 1
        assert any(name == "scan_company" for name, _ in enqueued)

    def test_scan_one_company(
        self, client: TestClient, company: Any, enqueued: list[Any]
    ) -> None:
        response = client.post("/api/v1/scan", json={"company_id": str(company.id)})
        assert response.json()["scheduled"] == 1

    def test_scan_unknown_company_returns_404(
        self, client: TestClient, enqueued: list[Any]
    ) -> None:
        import uuid

        response = client.post("/api/v1/scan", json={"company_id": str(uuid.uuid4())})
        assert response.status_code == 404

    def test_rescan_ignores_the_schedule(
        self, client: TestClient, company: Any, enqueued: list[Any], db_session: Session
    ) -> None:
        from datetime import timedelta

        from app.utils.time import utcnow

        company.next_scrape_at = utcnow() + timedelta(days=5)
        db_session.flush()

        response = client.post("/api/v1/rescan", json={})
        assert response.json()["scheduled"] == 1

    def test_force_llm_is_passed_through(
        self, client: TestClient, company: Any, enqueued: list[Any]
    ) -> None:
        client.post(
            "/api/v1/rescan", json={"company_id": str(company.id), "force_llm": True}
        )
        _, call = enqueued[-1]
        assert call["kwargs"]["force_llm"] is True


class TestNotificationsApi:
    def test_empty_log(self, client: TestClient) -> None:
        response = client.get("/api/v1/notifications")
        assert response.status_code == 200
        assert response.json()["total"] == 0

    def test_summary_handles_no_data(self, client: TestClient) -> None:
        body = client.get("/api/v1/notifications/summary").json()
        assert body["total"] == 0
        assert body["success_rate"] is None
