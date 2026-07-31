"""Add jobs.posted_at

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable and backfill-free: existing rows keep their date-only value and
    # simply report a coarser age until they are next re-scraped.
    op.add_column(
        "jobs", sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_jobs_posted_at", "jobs", ["posted_at"])


def downgrade() -> None:
    op.drop_index("ix_jobs_posted_at", table_name="jobs")
    op.drop_column("jobs", "posted_at")
