from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.enums import JobStatus
from app.models import Job, WorkerHeartbeat

DEFAULT_LEASE = timedelta(minutes=15)
RETRY_BACKOFF_SECONDS = (5, 20)


class LostLeaseError(RuntimeError):
    pass


Publisher = Callable[[AsyncSession, Job], Awaitable[None]]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class JobQueue:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        lease_duration: timedelta = DEFAULT_LEASE,
    ):
        self._sessions = sessions
        self._lease_duration = lease_duration

    async def enqueue(
        self,
        *,
        job_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        max_attempts: int = 3,
    ) -> tuple[Job, bool]:
        async with self._sessions.begin() as session:
            existing = await session.scalar(
                select(Job).where(Job.idempotency_key == idempotency_key)
            )
            if existing is not None:
                return existing, False
            job = Job(
                job_type=job_type,
                payload=payload,
                idempotency_key=idempotency_key,
                max_attempts=max_attempts,
                status=JobStatus.QUEUED.value,
            )
            session.add(job)
            await session.flush()
            return job, True

    async def claim(self, *, owner: str, now: datetime | None = None) -> Job | None:
        now = now or datetime.now(UTC)
        normal_ready = and_(
            Job.status.in_([JobStatus.QUEUED.value, JobStatus.FAILED_RETRYABLE.value]),
            Job.available_at <= now,
        )
        expired_running = and_(
            Job.status == JobStatus.RUNNING.value,
            Job.lease_expires_at.is_not(None),
            Job.lease_expires_at < now,
        )
        async with self._sessions.begin() as session:
            job = await session.scalar(
                select(Job)
                .where(or_(normal_ready, expired_running))
                .order_by(Job.available_at, Job.created_at, Job.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return None
            job.status = JobStatus.RUNNING.value
            job.lease_owner = owner
            job.lease_expires_at = now + self._lease_duration
            job.heartbeat_at = now
            job.attempt += 1
            job.error_code = None
            job.error_message = None
            await session.flush()
            return job

    async def heartbeat(self, *, job_id: str, owner: str, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.status == JobStatus.RUNNING.value,
                    Job.lease_owner == owner,
                    Job.lease_expires_at >= now,
                )
                .values(heartbeat_at=now, lease_expires_at=now + self._lease_duration)
            )
            return result.rowcount == 1

    async def lease_is_current(
        self, *, job_id: str, owner: str, now: datetime | None = None
    ) -> bool:
        now = now or datetime.now(UTC)
        async with self._sessions() as session:
            return bool(
                await session.scalar(
                    select(Job.id).where(
                        Job.id == job_id,
                        Job.status == JobStatus.RUNNING.value,
                        Job.lease_owner == owner,
                        Job.lease_expires_at >= now,
                    )
                )
            )

    async def complete(
        self,
        *,
        job_id: str,
        owner: str,
        publisher: Publisher | None = None,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(UTC)
        async with self._sessions.begin() as session:
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if (
                job is None
                or job.status != JobStatus.RUNNING.value
                or job.lease_owner != owner
                or job.lease_expires_at is None
                or _utc(job.lease_expires_at) < _utc(now)
            ):
                raise LostLeaseError(job_id)
            if publisher is not None:
                await publisher(session, job)
            job.status = JobStatus.SUCCEEDED.value
            job.lease_owner = None
            job.lease_expires_at = None
            job.heartbeat_at = now

    async def fail(
        self,
        *,
        job_id: str,
        owner: str,
        error_code: str,
        error_message: str,
        retryable: bool,
        now: datetime | None = None,
    ) -> JobStatus:
        now = now or datetime.now(UTC)
        async with self._sessions.begin() as session:
            job = await session.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if job is None or job.status != JobStatus.RUNNING.value or job.lease_owner != owner:
                raise LostLeaseError(job_id)
            can_retry = retryable and job.attempt < job.max_attempts
            if can_retry:
                delay_index = min(max(job.attempt - 1, 0), len(RETRY_BACKOFF_SECONDS) - 1)
                job.status = JobStatus.FAILED_RETRYABLE.value
                job.available_at = now + timedelta(seconds=RETRY_BACKOFF_SECONDS[delay_index])
            else:
                job.status = JobStatus.FAILED_FINAL.value
            job.error_code = error_code
            job.error_message = error_message[-2000:]
            job.lease_owner = None
            job.lease_expires_at = None
            job.heartbeat_at = now
            return JobStatus(job.status)

    async def retry(self, *, job_id: str, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.status.in_(
                        [JobStatus.FAILED_RETRYABLE.value, JobStatus.FAILED_FINAL.value]
                    ),
                )
                .values(
                    status=JobStatus.QUEUED.value,
                    attempt=0,
                    available_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                    error_code=None,
                    error_message=None,
                )
            )
            return result.rowcount == 1

    async def status(self, *, job_id: str) -> Job | None:
        async with self._sessions() as session:
            return await session.get(Job, job_id)

    async def record_worker_heartbeat(
        self, *, worker_id: str, isolation_ok: bool, now: datetime | None = None
    ) -> None:
        now = now or datetime.now(UTC)
        async with self._sessions.begin() as session:
            heartbeat = await session.get(WorkerHeartbeat, worker_id)
            if heartbeat is None:
                session.add(
                    WorkerHeartbeat(
                        worker_id=worker_id,
                        heartbeat_at=now,
                        isolation_ok=isolation_ok,
                    )
                )
            else:
                heartbeat.heartbeat_at = now
                heartbeat.isolation_ok = isolation_ok
