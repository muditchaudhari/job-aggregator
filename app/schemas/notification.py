from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import NotificationChannel, NotificationStatus
from app.schemas.common import ORMModel


class NotificationRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    user_id: uuid.UUID
    channel: NotificationChannel
    status: NotificationStatus
    sent_at: datetime | None
    attempts: int
    error: str | None
    match_score: float | None
    created_at: datetime
    #: Denormalised for display so a delivery log renders without a join.
    job_title: str | None = None
    job_url: str | None = None


class ScanRequest(BaseModel):
    """Body for ``POST /scan`` and ``POST /rescan``."""

    company_id: uuid.UUID | None = None
    #: Skip tiers 1-4 and go straight to selector regeneration. Costs an LLM
    #: call per company — intended for debugging a site whose markup changed.
    force_llm: bool = False
    #: Scan and store without delivering. Useful when re-testing extraction on
    #: a company that would otherwise re-notify.
    notify: bool = True
    limit: int = Field(default=100, ge=1, le=1000)


class ScanResponse(BaseModel):
    scheduled: int
    company_ids: list[uuid.UUID]
    message: str
