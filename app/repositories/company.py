from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from app.models.company import Company
from app.models.enums import ATSType
from app.repositories.base import BaseRepository
from app.utils.time import utcnow
from app.utils.urls import canonicalize_url


class CompanyRepository(BaseRepository[Company]):
    model = Company

    def get_by_url(self, career_url: str) -> Company | None:
        stmt = sa.select(Company).where(
            Company.career_url == canonicalize_url(career_url)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_filtered(
        self,
        *,
        ats_type: ATSType | None = None,
        is_active: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Company]:
        stmt = sa.select(Company)
        if ats_type is not None:
            stmt = stmt.where(Company.ats_type == ats_type)
        if is_active is not None:
            stmt = stmt.where(Company.is_active.is_(is_active))
        stmt = stmt.order_by(Company.name).limit(limit).offset(offset)
        return self.session.execute(stmt).scalars().all()

    def due_for_scrape(self, *, limit: int = 500) -> Sequence[Company]:
        """Companies whose next scrape time has passed.

        ``next_scrape_at IS NULL`` means never scraped, which must be treated as
        due — otherwise a newly registered company waits a full interval before
        its first scan and the user sees nothing after adding it.

        Ordered by ``next_scrape_at`` so the most overdue go first; when the
        fleet is behind, that degrades into "oldest first" rather than
        starving a subset of companies indefinitely.
        """
        stmt = (
            sa.select(Company)
            .where(
                Company.is_active.is_(True),
                sa.or_(
                    Company.next_scrape_at.is_(None),
                    Company.next_scrape_at <= utcnow(),
                ),
            )
            .order_by(Company.next_scrape_at.asc().nulls_first())
            .limit(limit)
        )
        return self.session.execute(stmt).scalars().all()

    def deactivate(self, company: Company, reason: str) -> Company:
        """Soft delete.

        Hard deletion would cascade away every job and every notification ever
        sent for this company, destroying the delivery history the user may
        still be reading.
        """
        company.is_active = False
        company.last_error = reason
        self.session.flush()
        return company

    def count_active(self) -> int:
        stmt = (
            sa.select(sa.func.count())
            .select_from(Company)
            .where(Company.is_active.is_(True))
        )
        return int(self.session.execute(stmt).scalar_one())
