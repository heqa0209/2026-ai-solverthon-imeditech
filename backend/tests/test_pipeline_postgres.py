from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.decision_service import publish_deterministic_decision
from app.jobs import enqueue_job
from app.models import (
    AnalysisRun,
    Announcement,
    AnnouncementVersion,
    Base,
    CompanyProfile,
    CompanyProfileVersion,
    EligibilityDecision,
    ExtractedCondition,
    Job,
    User,
)
from app.pipeline.jobs import JobQueue

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not configured")


def _assert_dedicated_database() -> None:
    database_name = make_url(POSTGRES_URL).database
    if not database_name or not database_name.startswith("solverthon_pipeline_test"):
        pytest.fail("TEST_POSTGRES_URL must target a dedicated solverthon_pipeline_test database")


async def _database():
    _assert_dedicated_database()
    engine = create_async_engine(POSTGRES_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    return engine, sessions


@pytest.mark.asyncio
async def test_postgres_skip_locked_and_lease_lifecycle() -> None:
    engine, sessions = await _database()
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


@pytest.mark.asyncio
async def test_postgres_concurrent_idempotent_job_inserts_return_one_row() -> None:
    engine, sessions = await _database()

    async def enqueue_from_api() -> str:
        async with sessions.begin() as session:
            return (await enqueue_job(session, "CONCURRENT_API", {"same": True})).id

    api_ids = await asyncio.gather(*(enqueue_from_api() for _ in range(8)))
    assert len(set(api_ids)) == 1

    queue = JobQueue(sessions)

    async def enqueue_from_queue() -> tuple[str, bool]:
        job, created = await queue.enqueue(
            job_type="CONCURRENT_QUEUE",
            payload={"same": True},
            idempotency_key="concurrent-queue-key",
        )
        return job.id, created

    queue_results = await asyncio.gather(*(enqueue_from_queue() for _ in range(8)))
    assert len({job_id for job_id, _ in queue_results}) == 1
    assert sum(created for _, created in queue_results) == 1
    async with sessions() as session:
        assert await session.scalar(select(func.count(Job.id))) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_concurrent_decision_publish_keeps_one_current_row() -> None:
    engine, sessions = await _database()
    async with sessions.begin() as session:
        user = User(username="decision-user", password_hash="test-only")
        session.add(user)
        await session.flush()
        profile = CompanyProfile(user_id=user.id)
        session.add(profile)
        await session.flush()
        profile_version = CompanyProfileVersion(
            profile_id=profile.id,
            user_id=user.id,
            version=1,
            snapshot={"companyName": "동시성 테스트", "employeeCount": 5},
            raw_input={"companyName": "동시성 테스트", "employeeCount": 5},
        )
        session.add(profile_version)
        await session.flush()
        profile.current_version_id = profile_version.id
        announcement = Announcement(
            source_id="concurrent-decision", source_url="https://example.test/concurrent"
        )
        session.add(announcement)
        await session.flush()
        version = AnnouncementVersion(
            announcement_id=announcement.id,
            raw_payload={},
            content_hash="c" * 64,
            title="동시 판정",
        )
        session.add(version)
        await session.flush()
        announcement.current_version_id = version.id
        analysis = AnalysisRun(
            announcement_version_id=version.id,
            status="SUCCEEDED",
            analysis_version="concurrency-v1",
            canonical_ir={
                "groups": [{"group_id": "root", "operator": "ALL"}],
                "conditions": [
                    {
                        "condition_id": "employee",
                        "group_id": "root",
                        "kind": "MANDATORY",
                        "subject": "EMPLOYEE_COUNT",
                        "operator": "LTE",
                        "expected_value": {"type": "INTEGER", "value": 10},
                    }
                ],
            },
        )
        session.add(analysis)
        await session.flush()
        session.add(
            ExtractedCondition(
                analysis_run_id=analysis.id,
                condition_key="employee",
                group_key="root",
                kind="MANDATORY",
                subject="EMPLOYEE_COUNT",
                operator="LTE",
                expected_value={"type": "INTEGER", "value": 10},
                evidence=[{"verbatim_text": "상시근로자 10인 이하"}],
            )
        )
        identifiers = {
            "user_id": user.id,
            "announcement_id": announcement.id,
            "announcement_version_id": version.id,
            "company_profile_version_id": profile_version.id,
            "analysis_run_id": analysis.id,
        }

    async def publish() -> str:
        async with sessions.begin() as session:
            decision = await publish_deterministic_decision(
                session,
                **identifiers,
                selected_role_key=None,
            )
            return decision.id

    decision_ids = await asyncio.gather(publish(), publish())
    assert len(set(decision_ids)) == 2
    async with sessions() as session:
        total = await session.scalar(select(func.count(EligibilityDecision.id)))
        current = await session.scalar(
            select(func.count(EligibilityDecision.id)).where(
                EligibilityDecision.user_id == identifiers["user_id"],
                EligibilityDecision.announcement_id == identifiers["announcement_id"],
                EligibilityDecision.is_current.is_(True),
            )
        )
        assert total == 2
        assert current == 1
    await engine.dispose()
