from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa

from app.models.enums import ScrapeStatus
from app.models.scrape_run import ScrapeRun
from app.repositories.base import BaseRepository
from app.utils.time import utcnow


class ScrapeRunRepository(BaseRepository[ScrapeRun]):
    model = ScrapeRun

    def latest_for_company(self, company_id: uuid.UUID) -> ScrapeRun | None:
        stmt = (
            sa.select(ScrapeRun)
            .where(ScrapeRun.company_id == company_id)
            .order_by(ScrapeRun.started_at.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def recent(self, *, limit: int = 50) -> Sequence[ScrapeRun]:
        stmt = sa.select(ScrapeRun).order_by(ScrapeRun.started_at.desc()).limit(limit)
        return self.session.execute(stmt).scalars().all()

    def summary(self, *, window_hours: int = 24) -> dict[str, Any]:
        """Aggregates backing ``GET /metrics``.

        Computed in SQL rather than by loading rows: at a thousand companies
        scanned hourly this window holds ~24k rows, and pulling them into
        Python to sum four columns would be silly.
        """
        since = utcnow() - timedelta(hours=window_hours)
        row = self.session.execute(
            sa.select(
                sa.func.count().label("runs"),
                sa.func.coalesce(sa.func.sum(ScrapeRun.jobs_found), 0),
                sa.func.coalesce(sa.func.sum(ScrapeRun.jobs_new), 0),
                sa.func.coalesce(sa.func.sum(ScrapeRun.jobs_duplicate), 0),
                sa.func.coalesce(sa.func.sum(ScrapeRun.llm_calls), 0),
                sa.func.coalesce(sa.func.sum(ScrapeRun.llm_cost_usd), 0.0),
                sa.func.coalesce(sa.func.avg(ScrapeRun.total_ms), 0.0),
            ).where(ScrapeRun.started_at >= since)
        ).one()

        failures = int(
            self.session.execute(
                sa.select(sa.func.count())
                .select_from(ScrapeRun)
                .where(
                    ScrapeRun.started_at >= since,
                    ScrapeRun.status == ScrapeStatus.FAILED,
                )
            ).scalar_one()
        )

        tiers = {
            str(tier): int(count)
            for tier, count in self.session.execute(
                sa.select(ScrapeRun.extraction_tier, sa.func.count())
                .where(ScrapeRun.started_at >= since)
                .group_by(ScrapeRun.extraction_tier)
            )
        }

        return {
            "window_hours": window_hours,
            "runs": int(row[0]),
            "pages_scraped": int(row[0]),
            "failed_pages": failures,
            "jobs_found": int(row[1]),
            "new_jobs": int(row[2]),
            "duplicates": int(row[3]),
            "llm_calls": int(row[4]),
            "llm_cost_usd": round(float(row[5]), 4),
            "avg_scrape_ms": round(float(row[6]), 1),
            "extraction_tiers": tiers,
        }

    def open_run(self, company_id: uuid.UUID) -> ScrapeRun:
        run = ScrapeRun(
            company_id=company_id,
            started_at=utcnow(),
            status=ScrapeStatus.RUNNING,
        )
        return self.add(run)

    def close_run(
        self, run: ScrapeRun, status: ScrapeStatus, error: str | None = None
    ) -> ScrapeRun:
        finished: datetime = utcnow()
        run.finished_at = finished
        run.status = status
        run.error = error
        run.total_ms = int((finished - run.started_at).total_seconds() * 1000)
        self.session.flush()
        return run
