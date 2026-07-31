"""Initial schema.

Revision ID: 0001
Revises:
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("career_url", sa.String(1024), nullable=False),
        sa.Column("website", sa.String(255), nullable=False),
        sa.Column("ats_type", sa.String(32), nullable=False),
        sa.Column("scraping_strategy", sa.String(32), nullable=False),
        sa.Column("board_token", sa.String(255), nullable=True),
        sa.Column("api_endpoint", sa.String(1024), nullable=True),
        sa.Column("scrape_frequency", sa.String(16), nullable=False),
        sa.Column("scrape_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("last_scraped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_scrape_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "scrape_interval_minutes > 0",
            name=op.f("ck_companies_scrape_interval_positive"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_companies")),
        sa.UniqueConstraint("career_url", name=op.f("uq_companies_career_url")),
    )
    op.create_index("ix_companies_due", "companies", ["is_active", "next_scrape_at"])
    op.create_index(
        op.f("ix_companies_career_url"), "companies", ["career_url"], unique=True
    )
    op.create_index(op.f("ix_companies_website"), "companies", ["website"])
    op.create_index(op.f("ix_companies_next_scrape_at"), "companies", ["next_scrape_at"])

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    op.create_table(
        "user_profiles",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("preferred_roles", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("preferred_locations", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("preferred_skills", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("industries", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("excluded_keywords", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("seniority", sa.String(16), nullable=False),
        sa.Column("remote_preference", sa.String(16), nullable=False),
        sa.Column("years_experience", sa.Integer(), nullable=True),
        sa.Column("requires_visa_sponsorship", sa.Boolean(), nullable=False),
        sa.Column("desired_salary_min", sa.Integer(), nullable=True),
        sa.Column("salary_currency", sa.String(8), nullable=True),
        sa.Column("match_threshold", sa.Float(), nullable=False),
        sa.Column("include_unknown_location", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_profiles_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_profiles")),
        sa.UniqueConstraint("user_id", name=op.f("uq_user_profiles_user_id")),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("external_job_id", sa.String(255), nullable=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("location_raw", sa.String(512), nullable=True),
        sa.Column("location_city", sa.String(255), nullable=True),
        sa.Column("location_region", sa.String(255), nullable=True),
        sa.Column("location_country", sa.String(128), nullable=True),
        sa.Column("remote_type", sa.String(16), nullable=False),
        sa.Column("employment_type", sa.String(16), nullable=False),
        sa.Column("seniority", sa.String(16), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("requirements", sa.Text(), nullable=True),
        sa.Column("detected_skills", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("salary_min", sa.Integer(), nullable=True),
        sa.Column("salary_max", sa.Integer(), nullable=True),
        sa.Column("salary_currency", sa.String(8), nullable=True),
        sa.Column("salary_period", sa.String(16), nullable=False),
        sa.Column("salary_raw", sa.String(255), nullable=True),
        sa.Column("posted_date", sa.Date(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("raw_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("extraction_tier", sa.String(32), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_jobs_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint("company_id", "content_hash", name="uq_jobs_company_hash"),
    )
    op.create_index(op.f("ix_jobs_company_id"), "jobs", ["company_id"])
    op.create_index(op.f("ix_jobs_content_hash"), "jobs", ["content_hash"])
    op.create_index("ix_jobs_first_seen", "jobs", ["first_seen_at"])
    op.create_index("ix_jobs_company_active", "jobs", ["company_id", "is_active"])
    op.create_index("ix_jobs_posted", "jobs", ["posted_date"])

    op.create_table(
        "job_matches",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("job_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("profile_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("matched_skills", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("missing_skills", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("matcher", sa.String(32), nullable=False),
        sa.Column("matcher_version", sa.String(32), nullable=False),
        sa.Column("is_match", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_job_matches_job_id_jobs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["user_profiles.id"],
            name=op.f("fk_job_matches_profile_id_user_profiles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_matches")),
        sa.UniqueConstraint("job_id", "profile_id", name="uq_job_matches_job_profile"),
    )
    op.create_index(op.f("ix_job_matches_job_id"), "job_matches", ["job_id"])
    op.create_index(op.f("ix_job_matches_profile_id"), "job_matches", ["profile_id"])
    op.create_index("ix_job_matches_score", "job_matches", ["score"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("job_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("match_score", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_notifications_job_id_jobs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notifications_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint(
            "job_id", "user_id", "channel", name="uq_notifications_job_user_channel"
        ),
    )
    op.create_index(op.f("ix_notifications_job_id"), "notifications", ["job_id"])
    op.create_index(op.f("ix_notifications_user_id"), "notifications", ["user_id"])
    op.create_index("ix_notifications_status", "notifications", ["status", "created_at"])

    op.create_table(
        "selectors",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("website", sa.String(255), nullable=False),
        sa.Column("selector_version", sa.Integer(), nullable=False),
        sa.Column("strategy", sa.String(16), nullable=False),
        sa.Column("container_selector", sa.String(512), nullable=True),
        sa.Column("title_selector", sa.String(512), nullable=True),
        sa.Column("url_selector", sa.String(512), nullable=True),
        sa.Column("location_selector", sa.String(512), nullable=True),
        sa.Column("description_selector", sa.String(512), nullable=True),
        sa.Column("date_selector", sa.String(512), nullable=True),
        sa.Column("department_selector", sa.String(512), nullable=True),
        sa.Column("json_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("requires_render", sa.Boolean(), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("llm_model", sa.String(128), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name=op.f("ck_selectors_confidence_in_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_selectors")),
        sa.UniqueConstraint(
            "website", "selector_version", name="uq_selectors_website_version"
        ),
    )
    op.create_index(op.f("ix_selectors_website"), "selectors", ["website"])
    op.create_index("ix_selectors_active", "selectors", ["website", "is_active"])

    op.create_table(
        "scrape_runs",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("company_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("strategy_used", sa.String(32), nullable=True),
        sa.Column("extraction_tier", sa.String(32), nullable=True),
        sa.Column("selector_version", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("jobs_found", sa.Integer(), nullable=False),
        sa.Column("jobs_new", sa.Integer(), nullable=False),
        sa.Column("jobs_duplicate", sa.Integer(), nullable=False),
        sa.Column("notifications_sent", sa.Integer(), nullable=False),
        sa.Column("fetch_ms", sa.Integer(), nullable=True),
        sa.Column("render_ms", sa.Integer(), nullable=True),
        sa.Column("total_ms", sa.Integer(), nullable=True),
        sa.Column("llm_calls", sa.Integer(), nullable=False),
        sa.Column("llm_tokens_in", sa.Integer(), nullable=False),
        sa.Column("llm_tokens_out", sa.Integer(), nullable=False),
        sa.Column("llm_cost_usd", sa.Float(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_scrape_runs_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scrape_runs")),
    )
    op.create_index(op.f("ix_scrape_runs_company_id"), "scrape_runs", ["company_id"])
    op.create_index(
        "ix_scrape_runs_company_time", "scrape_runs", ["company_id", "started_at"]
    )
    op.create_index("ix_scrape_runs_status", "scrape_runs", ["status", "started_at"])


def downgrade() -> None:
    op.drop_table("scrape_runs")
    op.drop_table("selectors")
    op.drop_table("notifications")
    op.drop_table("job_matches")
    op.drop_table("jobs")
    op.drop_table("user_profiles")
    op.drop_table("users")
    op.drop_table("companies")
