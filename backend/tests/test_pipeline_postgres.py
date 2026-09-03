from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Job
from app.pipeline.jobs import JobQueue

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not configured")


@pytest.mark.asyncio
async def test_postgres_skip_locked_and_lease_lifecycle() -> None:
    database_name = make_url(POSTGRES_URL).database
    if not database_name or not database_name.startswith("solverthon_pipeline_test"):
        pytest.fail("TEST_POSTGRES_URL must target a dedicated solverthon_pipeline_test database")
    engine = create_async_engine(POSTGRES_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    queue = JobQueue(sessions, lease_duration=timedelta(minutes=15))
    first, _ = await queue.enqueue(job_type="TEST", payload={"index": 1}, idempotency_key="1")
    second, _ = await queue.enqueue(job_type="TEST", payload={"index": 2}, idempotency_key="2")

    async with sessions() as session_one, sessions() as session_two:
        await session_one.begin()
        locked = await session_one.scalar(
            select(Job).order_by(Job.created_at, Job.id).limit(1).with_for_update(skip_locked=True)
        )
        await session_two.begin()
        skipped = await session_two.scalar(
            select(Job).order_by(Job.created_at, Job.id).limit(1).with_for_update(skip_locked=True)
        )
        assert locked is not None and skipped is not None
        assert {locked.id, skipped.id} == {first.id, second.id}
        await session_two.rollback()
        await session_one.rollback()

    now = datetime.now(UTC)
    claimed = await queue.claim(owner="postgres-worker", now=now)
    assert claimed is not None
    assert await queue.heartbeat(
        job_id=claimed.id,
        owner="postgres-worker",
        now=now + timedelta(seconds=30),
    )
    await queue.complete(
        job_id=claimed.id,
        owner="postgres-worker",
        now=now + timedelta(seconds=31),
    )
    completed = await queue.status(job_id=claimed.id)
    assert completed is not None and completed.status == "SUCCEEDED"
    await engine.dispose()
