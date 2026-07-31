from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.enums import RemoteType
from app.models.job import Job
from app.repositories.base import BaseRepository
from app.utils.time import utcnow


def _row_values(job: Job) -> dict[str, object]:
    """Flatten a ``Job`` into INSERT parameters.

    Needed because the bulk path bypasses the ORM's flush, and with it the
    machinery that applies Python-side column defaults. Naively taking only
    the non-``None`` attributes would silently omit ``is_active``,
    ``raw_json``, and friends — every one of them ``NOT NULL`` — and the
    insert would fail on the first batch.

    Columns backed by a *server* default are deliberately left out so the
    database supplies them.
    """
    values: dict[str, object] = {}
    for column in Job.__table__.columns:
        value = getattr(job, column.key, None)
        if value is not None:
            values[column.key] = value
            continue
        if column.default is not None and not column.default.is_sequence:
            arg = column.default.arg
            values[column.key] = arg(None) if callable(arg) else arg
        elif column.server_default is None and column.nullable:
            values[column.key] = None
        # else: NOT NULL with a server default — let the database fill it in.
    return values


class JobRepository(BaseRepository[Job]):
    model = Job

    # -- Deduplicating write path -----------------------------------------

    def insert_new(self, jobs: Sequence[Job]) -> list[Job]:
        """Insert postings, skipping ones already known. Returns only the new.

        This is the beating heart of deduplication (AD-6). The uniqueness
        decision is made by the database, not by a prior ``SELECT``, so two
        workers scanning the same company concurrently cannot both conclude a
        posting is new.

        On PostgreSQL this is a single ``INSERT ... ON CONFLICT DO NOTHING
        RETURNING id``. SQLite (unit tests) has no equivalent that returns the
        affected rows reliably across versions, so it falls back to a
        filter-then-insert — correct in a single-threaded test, and never used
        in production.
        """
        if not jobs:
            return []

        if self.session.bind is None or self.session.bind.dialect.name != "postgresql":
            return self._insert_new_portable(jobs)

        # Assigned up front, not left to the ORM: this path issues a Core
        # INSERT, so nothing would populate the primary key, and the ids are
        # needed to match the RETURNING rows back to the objects.
        for job in jobs:
            if job.id is None:
                job.id = uuid.uuid4()

        payload = [_row_values(job) for job in jobs]
        stmt = (
            pg_insert(Job)
            .values(payload)
            .on_conflict_do_nothing(constraint="uq_jobs_company_hash")
            .returning(Job.id)
        )
        inserted_ids = set(self.session.execute(stmt).scalars().all())
        self.session.flush()
        if not inserted_ids:
            return []

        # Re-select rather than returning the objects we built. A Core INSERT
        # does not attach anything to the session, so those objects are
        # transient: later mutations (detail enrichment filling in a
        # description, a corrected seniority) look like they worked and are
        # silently discarded at commit. Handing back persistent instances makes
        # "the caller may keep editing these" actually true.
        persistent = (
            self.session.execute(sa.select(Job).where(Job.id.in_(inserted_ids)))
            .scalars()
            .all()
        )
        order = {job_id: index for index, job_id in enumerate(j.id for j in jobs)}
        return sorted(persistent, key=lambda job: order.get(job.id, 0))

    def _insert_new_portable(self, jobs: Sequence[Job]) -> list[Job]:
        by_company: dict[uuid.UUID, set[str]] = {}
        for job in jobs:
            by_company.setdefault(job.company_id, set()).add(job.content_hash)

        existing: set[tuple[uuid.UUID, str]] = set()
        for company_id, hashes in by_company.items():
            rows = self.session.execute(
                sa.select(Job.company_id, Job.content_hash).where(
                    Job.company_id == company_id, Job.content_hash.in_(hashes)
                )
            ).all()
            existing.update((row[0], row[1]) for row in rows)

        fresh: list[Job] = []
        seen_in_batch: set[tuple[uuid.UUID, str]] = set()
        for job in jobs:
            key = (job.company_id, job.content_hash)
            if key in existing or key in seen_in_batch:
                continue
            seen_in_batch.add(key)
            fresh.append(job)

        self.session.add_all(fresh)
        self.session.flush()
        return fresh

    def touch_seen(self, company_id: uuid.UUID, hashes: Sequence[str]) -> None:
        """Mark still-listed postings as seen in this run.

        Drives ``is_active`` reaping: a posting that stops appearing gets an
        increasingly stale ``last_seen_at`` and is eventually closed out,
        without ever being deleted.
        """
        if not hashes:
            return
        self.session.execute(
            sa.update(Job)
            .where(Job.company_id == company_id, Job.content_hash.in_(list(hashes)))
            .values(last_seen_at=utcnow())
        )

    def deactivate_stale(self, company_id: uuid.UUID, *, older_than_days: int = 7) -> int:
        cutoff = utcnow() - timedelta(days=older_than_days)
        result = self.session.execute(
            sa.update(Job)
            .where(
                Job.company_id == company_id,
                Job.is_active.is_(True),
                Job.last_seen_at < cutoff,
            )
            .values(is_active=False)
        )
        return int(result.rowcount or 0)

    # -- Read path ---------------------------------------------------------

    def list_filtered(
        self,
        *,
        company_id: uuid.UUID | None = None,
        location: str | None = None,
        remote_type: RemoteType | None = None,
        title_contains: str | None = None,
        is_active: bool | None = True,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[Job], int]:
        """Filtered page plus the unpaginated total.

        Returned together because the caller needs both for a pagination
        envelope, and issuing them as two independent calls invites the count
        and the page to be computed against different snapshots.
        """
        conditions: list[sa.ColumnElement[bool]] = []
        if company_id is not None:
            conditions.append(Job.company_id == company_id)
        if is_active is not None:
            conditions.append(Job.is_active.is_(is_active))
        if remote_type is not None:
            conditions.append(Job.remote_type == remote_type)
        if location:
            pattern = f"%{location.lower()}%"
            conditions.append(
                sa.or_(
                    sa.func.lower(Job.location_raw).like(pattern),
                    sa.func.lower(Job.location_city).like(pattern),
                    sa.func.lower(Job.location_country).like(pattern),
                )
            )
        if title_contains:
            conditions.append(sa.func.lower(Job.title).like(f"%{title_contains.lower()}%"))
        if since is not None:
            conditions.append(Job.first_seen_at >= since)

        total = int(
            self.session.execute(
                sa.select(sa.func.count()).select_from(Job).where(*conditions)
            ).scalar_one()
        )
        rows = (
            self.session.execute(
                sa.select(Job)
                .where(*conditions)
                .order_by(Job.first_seen_at.desc())
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        return rows, total

    def list_new_since(
        self, since: datetime, *, limit: int = 50, offset: int = 0
    ) -> tuple[Sequence[Job], int]:
        return self.list_filtered(since=since, limit=limit, offset=offset)

    def get_by_hash(self, company_id: uuid.UUID, content_hash: str) -> Job | None:
        return self.session.execute(
            sa.select(Job).where(
                Job.company_id == company_id, Job.content_hash == content_hash
            )
        ).scalar_one_or_none()

    def count_since(self, since: datetime) -> int:
        return int(
            self.session.execute(
                sa.select(sa.func.count())
                .select_from(Job)
                .where(Job.first_seen_at >= since)
            ).scalar_one()
        )
