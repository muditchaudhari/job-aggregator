from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from app.models.selector import Selector
from app.repositories.base import BaseRepository


class SelectorRepository(BaseRepository[Selector]):
    model = Selector

    def get_active(self, website: str) -> Selector | None:
        """The selector version currently in force for a domain.

        Ordered by version descending as a safety net: a bug that leaves two
        rows active should resolve to the newer one rather than raising and
        taking the scan down with it.
        """
        stmt = (
            sa.select(Selector)
            .where(Selector.website == website, Selector.is_active.is_(True))
            .order_by(Selector.selector_version.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_versions(self, website: str) -> Sequence[Selector]:
        stmt = (
            sa.select(Selector)
            .where(Selector.website == website)
            .order_by(Selector.selector_version.desc())
        )
        return self.session.execute(stmt).scalars().all()

    def next_version(self, website: str) -> int:
        stmt = sa.select(sa.func.max(Selector.selector_version)).where(
            Selector.website == website
        )
        current = self.session.execute(stmt).scalar()
        return int(current or 0) + 1

    def deactivate_all(self, website: str) -> None:
        self.session.execute(
            sa.update(Selector)
            .where(Selector.website == website, Selector.is_active.is_(True))
            .values(is_active=False)
        )

    def promote(self, selector: Selector) -> Selector:
        """Make ``selector`` the single active version for its domain."""
        self.deactivate_all(selector.website)
        selector.is_active = True
        self.session.add(selector)
        self.session.flush()
        return selector

    def prune(self, website: str, keep: int) -> int:
        """Drop the oldest inactive versions beyond the retention limit.

        Versioning is what makes regressions revertible (AD-4), but a site that
        churns weekly would otherwise accumulate rows forever. Active versions
        are never pruned regardless of age.
        """
        versions = [s for s in self.list_versions(website) if not s.is_active]
        doomed = versions[keep:]
        for selector in doomed:
            self.session.delete(selector)
        if doomed:
            self.session.flush()
        return len(doomed)

    def list_degraded(self, *, threshold: float) -> Sequence[Selector]:
        """Active selectors whose confidence has fallen below a threshold.

        Used by the maintenance task to relearn sites proactively, off the
        critical path of a user-facing scan.
        """
        stmt = (
            sa.select(Selector)
            .where(
                Selector.is_active.is_(True),
                Selector.confidence_score < threshold,
            )
            .order_by(Selector.confidence_score.asc())
        )
        return self.session.execute(stmt).scalars().all()
