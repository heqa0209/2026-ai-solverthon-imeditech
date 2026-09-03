from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.jobs import enqueue_job, idempotency_key
from app.models import CollectionSnapshot, Job

SEOUL = ZoneInfo("Asia/Seoul")


def _latest_daily_due(now: datetime) -> datetime:
    local = now.astimezone(SEOUL)
    due = datetime.combine(local.date(), time(6), tzinfo=SEOUL)
    return due if local >= due else due - timedelta(days=1)


def _latest_weekly_due(now: datetime) -> datetime:
    daily_due = _latest_daily_due(now)
    return daily_due - timedelta(days=daily_due.weekday())


async def _enqueue_once(db: AsyncSession, job_type: str, payload: dict[str, object]) -> bool:
    key = idempotency_key(job_type, payload)
    if await db.scalar(select(Job.id).where(Job.idempotency_key == key)) is not None:
        return False
    await enqueue_job(db, job_type, payload)
    return True


async def enqueue_due_collection_jobs(
    sessions: async_sessionmaker[AsyncSession], *, now: datetime | None = None
) -> int:
    """Catch up the latest 06:00 daily and Monday full collection exactly once."""

    now = (now or datetime.now(UTC)).astimezone(UTC)
    due = (("DAILY", _latest_daily_due(now)), ("FULL", _latest_weekly_due(now)))
    enqueued = 0
    async with sessions.begin() as db:
        for scope, scheduled_for in due:
            latest = await db.scalar(
                select(CollectionSnapshot)
                .where(CollectionSnapshot.scope == scope, CollectionSnapshot.complete.is_(True))
                .order_by(desc(CollectionSnapshot.succeeded_at))
                .limit(1)
            )
            latest_at = latest.succeeded_at if latest is not None else None
            if latest_at is not None and latest_at.tzinfo is None:
                latest_at = latest_at.replace(tzinfo=UTC)
            if latest_at is not None and latest_at >= scheduled_for.astimezone(UTC):
                continue
            job_type = "BIZINFO_COLLECT" if scope == "DAILY" else "BIZINFO_RECONCILE"
            enqueued += await _enqueue_once(
                db,
                job_type,
                {
                    "scope": scope,
                    "scheduledFor": scheduled_for.isoformat(),
                },
            )
    return enqueued
