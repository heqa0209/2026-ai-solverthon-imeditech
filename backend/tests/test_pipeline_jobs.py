from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.enums import JobStatus
from app.models import Base
from app.pipeline.jobs import JobQueue, LostLeaseError
from app.pipeline.worker import JobOutcome, Worker


@pytest.fixture
async def queue() -> JobQueue:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield JobQueue(sessions, lease_duration=timedelta(minutes=15))
    await engine.dispose()


@pytest.mark.asyncio
async def test_idempotent_enqueue_claim_heartbeat_and_complete(queue: JobQueue) -> None:
    first, created = await queue.enqueue(
        job_type="analyze",
        payload={"announcementVersionId": "v1"},
        idempotency_key="analyze:v1",
    )
    duplicate, duplicate_created = await queue.enqueue(
        job_type="analyze",
        payload={"announcementVersionId": "v1"},
        idempotency_key="analyze:v1",
    )
    assert created is True
    assert duplicate_created is False
    assert duplicate.id == first.id

    now = datetime.now(UTC)
    claimed = await queue.claim(owner="worker-1", now=now)
    assert claimed is not None
    assert claimed.status == JobStatus.RUNNING
    assert claimed.attempt == 1
    assert await queue.heartbeat(
        job_id=claimed.id, owner="worker-1", now=now + timedelta(seconds=30)
    )

    async def publish(_session, locked_job) -> None:
        locked_job.payload = {"published": True}

    await queue.complete(
        job_id=claimed.id,
        owner="worker-1",
        publisher=publish,
        now=now + timedelta(seconds=31),
    )
    completed = await queue.status(job_id=claimed.id)
    assert completed is not None
    assert completed.status == JobStatus.SUCCEEDED
    assert completed.payload == {"published": True}


@pytest.mark.asyncio
async def test_retry_backoff_and_final_failure(queue: JobQueue) -> None:
    job, _ = await queue.enqueue(
        job_type="analyze", payload={}, idempotency_key="retry", max_attempts=2
    )
    now = datetime.now(UTC)
    claimed = await queue.claim(owner="worker", now=now)
    assert claimed is not None
    status = await queue.fail(
        job_id=job.id,
        owner="worker",
        error_code="NETWORK",
        error_message="temporary",
        retryable=True,
        now=now,
    )
    assert status is JobStatus.FAILED_RETRYABLE
    assert await queue.claim(owner="worker", now=now + timedelta(seconds=4)) is None
    second = await queue.claim(owner="worker", now=now + timedelta(seconds=5))
    assert second is not None
    status = await queue.fail(
        job_id=job.id,
        owner="worker",
        error_code="NETWORK",
        error_message="still failing",
        retryable=True,
        now=now + timedelta(seconds=5),
    )
    assert status is JobStatus.FAILED_FINAL
    assert await queue.retry(job_id=job.id, now=now + timedelta(seconds=6)) is True
    retried = await queue.claim(owner="worker", now=now + timedelta(seconds=6))
    assert retried is not None
    assert retried.attempt == 1


@pytest.mark.asyncio
async def test_expired_lease_can_be_reclaimed_and_old_owner_cannot_commit(queue: JobQueue) -> None:
    job, _ = await queue.enqueue(job_type="analyze", payload={}, idempotency_key="lease")
    now = datetime.now(UTC)
    assert await queue.claim(owner="old", now=now) is not None
    reclaimed = await queue.claim(owner="new", now=now + timedelta(minutes=16))
    assert reclaimed is not None
    assert reclaimed.lease_owner == "new"
    with pytest.raises(LostLeaseError):
        await queue.complete(job_id=job.id, owner="old", now=now + timedelta(minutes=16))


@pytest.mark.asyncio
async def test_worker_runs_jobs_with_independent_handler_and_marks_success(queue: JobQueue) -> None:
    seen: list[str] = []

    async def handler(payload: dict, _context) -> JobOutcome:
        seen.append(payload["id"])
        return JobOutcome()

    for index in range(3):
        await queue.enqueue(
            job_type="fixture",
            payload={"id": str(index)},
            idempotency_key=f"fixture:{index}",
        )
    worker = Worker(
        worker_id="worker",
        queue=queue,
        handlers={"fixture": handler},
        concurrency=5,
        heartbeat_seconds=0.01,
        isolation_check=_isolation_ok,
    )
    assert await worker.run_once() == 3
    assert sorted(seen) == ["0", "1", "2"]


@pytest.mark.asyncio
async def test_worker_does_not_fallback_when_handler_is_missing(queue: JobQueue) -> None:
    job, _ = await queue.enqueue(job_type="unknown", payload={}, idempotency_key="unknown")
    worker = Worker(worker_id="worker", queue=queue, handlers={}, isolation_check=_isolation_ok)
    await worker.run_once()
    failed = await queue.status(job_id=job.id)
    assert failed is not None
    assert failed.status == JobStatus.FAILED_FINAL
    assert failed.error_code == "JOB_HANDLER_MISSING"


async def _isolation_ok() -> bool:
    return True


@pytest.mark.asyncio
async def test_worker_refuses_to_claim_without_isolation_self_test(queue: JobQueue) -> None:
    job, _ = await queue.enqueue(job_type="fixture", payload={}, idempotency_key="unisolated")
    worker = Worker(worker_id="worker", queue=queue, handlers={})
    with pytest.raises(RuntimeError, match="not configured"):
        await worker.run_once()
    queued = await queue.status(job_id=job.id)
    assert queued is not None
    assert queued.status == JobStatus.QUEUED
