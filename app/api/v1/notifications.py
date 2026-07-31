from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Query

from app.api.deps import DbSession, Pagination
from app.models.enums import NotificationChannel, NotificationStatus
from app.repositories.notification import NotificationRepository
from app.schemas.common import Page
from app.schemas.notification import NotificationRead
from app.utils.time import utcnow

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=Page[NotificationRead])
def list_notifications(
    session: DbSession,
    page: Pagination,
    channel: NotificationChannel | None = Query(default=None),
    status_filter: NotificationStatus | None = Query(default=None, alias="status"),
    hours: int | None = Query(default=None, ge=1, le=8760),
) -> Page[NotificationRead]:
    """Delivery log.

    Failed deliveries are included by default rather than hidden — "why didn't
    I get an email?" is the main reason anyone opens this endpoint, and the
    answer is usually a failed row with an error on it.
    """
    repository = NotificationRepository(session)
    since = utcnow() - timedelta(hours=hours) if hours else None

    records, total = repository.list_filtered(
        channel=channel,
        status=status_filter,
        since=since,
        limit=page.limit,
        offset=page.offset,
    )

    items: list[NotificationRead] = []
    for record in records:
        item = NotificationRead.model_validate(record)
        if record.job is not None:
            item.job_title = record.job.title
            item.job_url = record.job.url
        items.append(item)

    return Page[NotificationRead](
        items=items, total=total, limit=page.limit, offset=page.offset
    )


@router.get("/summary")
def notification_summary(
    session: DbSession, hours: int = Query(default=24, ge=1, le=8760)
) -> dict[str, object]:
    since = utcnow() - timedelta(hours=hours)
    counts = NotificationRepository(session).count_by_status(since=since)
    total = sum(counts.values())
    sent = counts.get(str(NotificationStatus.SENT), 0)
    return {
        "window_hours": hours,
        "by_status": counts,
        "total": total,
        "success_rate": round(sent / total, 4) if total else None,
    }
