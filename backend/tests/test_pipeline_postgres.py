from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.decision_lock import serialize_decision_state
from app.decision_service import publish_deterministic_decision
from app.domain.eligibility import Evaluation
from app.enums import ConditionStatus
from app.jobs import enqueue_job
from app.models import (
    AIStageRun,
    AnalysisRun,
    Announcement,
    AnnouncementAnswer,
    AnnouncementVersion,
    Base,
    CompanyProfile,
    CompanyProfileVersion,
    EligibilityDecision,
    ExtractedCondition,
    Job,
    User,
)
from app.pipeline.handlers import decision_reevaluate_handler
from app.pipeline.jobs import JobQueue
from app.pipeline.semantic import semantic_answer_fingerprint

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


@pytest.mark.asyncio
async def test_postgres_new_answer_decision_cannot_be_overwritten_by_stale_fast_reevaluation() -> (
    None
):
    engine, sessions = await _database()
    answer_a = {"value": {"detail": "답변 A"}, "source": "USER_VERIFIED", "memo": None}
    async with sessions.begin() as session:
        user = User(username="semantic-race-user", password_hash="test-only")
        session.add(user)
        await session.flush()
        profile = CompanyProfile(user_id=user.id)
        session.add(profile)
        await session.flush()
        profile_version = CompanyProfileVersion(
            profile_id=profile.id,
            user_id=user.id,
            version=1,
            snapshot={"companyName": "의미판단 경합 테스트"},
            raw_input={"companyName": "의미판단 경합 테스트"},
        )
        session.add(profile_version)
        await session.flush()
        profile.current_version_id = profile_version.id
        announcement = Announcement(
            source_id="semantic-race", source_url="https://example.test/semantic-race"
        )
        session.add(announcement)
        await session.flush()
        version = AnnouncementVersion(
            announcement_id=announcement.id,
            raw_payload={},
            content_hash="s" * 64,
            title="의미판단 경합",
        )
        session.add(version)
        await session.flush()
        announcement.current_version_id = version.id
        analysis = AnalysisRun(
            announcement_version_id=version.id,
            status="SUCCEEDED",
            analysis_version="semantic-race-v1",
            completed_at=datetime.now(UTC),
            canonical_ir={
                "groups": [{"group_id": "root", "operator": "ALL"}],
                "conditions": [
                    {
                        "condition_id": "semantic-fit",
                        "group_id": "root",
                        "kind": "MANDATORY",
                        "subject": "OTHER",
                        "operator": "SEMANTIC_MATCH",
                        "expected_value": {"type": "STRING", "value": "의료기기 역량"},
                    }
                ],
            },
        )
        session.add(analysis)
        await session.flush()
        condition = ExtractedCondition(
            analysis_run_id=analysis.id,
            condition_key="semantic-fit",
            group_key="root",
            kind="MANDATORY",
            subject="OTHER",
            operator="SEMANTIC_MATCH",
            expected_value={"type": "STRING", "value": "의료기기 역량"},
            evidence=[{"verbatim_text": "의료기기 역량 보유 기업"}],
        )
        session.add(condition)
        await session.flush()
        session.add(
            AnnouncementAnswer(
                user_id=user.id,
                announcement_version_id=version.id,
                condition_id=condition.id,
                **answer_a,
            )
        )
        session.add(
            AIStageRun(
                analysis_run_id=analysis.id,
                company_profile_version_id=profile_version.id,
                stage="SEMANTIC_JUDGMENT",
                model="fake",
                effort="low",
                prompt_version="test",
                schema_version="test",
                input_hash="a" * 64,
                structured_output={
                    "condition_id": "semantic-fit",
                    "status": "PASS",
                    "answer_fingerprint": semantic_answer_fingerprint(answer_a),
                },
                evidence=[],
                duration_ms=1,
                attempt=1,
            )
        )
        identifiers = {
            "user_id": user.id,
            "announcement_id": announcement.id,
            "announcement_version_id": version.id,
            "company_profile_version_id": profile_version.id,
            "analysis_run_id": analysis.id,
        }
        condition_id = condition.id

    handler = decision_reevaluate_handler(sessions)
    stale_outcome = await handler(
        {
            "userId": identifiers["user_id"],
            "announcementId": identifiers["announcement_id"],
            "announcementVersionId": identifiers["announcement_version_id"],
            "companyProfileVersionId": identifiers["company_profile_version_id"],
            "cause": "ANSWER_SAVED",
        },
        None,
    )
    assert stale_outcome.publisher is not None

    newer_holds_lock = asyncio.Event()
    release_newer = asyncio.Event()

    async def publish_newer_answer() -> str:
        async with sessions.begin() as session:
            await serialize_decision_state(
                session,
                user_id=identifiers["user_id"],
                announcement_id=identifiers["announcement_id"],
            )
            session.add(
                AnnouncementAnswer(
                    user_id=identifiers["user_id"],
                    announcement_version_id=identifiers["announcement_version_id"],
                    condition_id=condition_id,
                    value={"detail": "답변 B"},
                    source="USER_VERIFIED",
                    memo=None,
                )
            )
            await session.flush()
            decision = await publish_deterministic_decision(
                session,
                **identifiers,
                selected_role_key=None,
                semantic_evaluations={
                    "semantic-fit": Evaluation(ConditionStatus.FAIL, explanation="답변 B")
                },
            )
            newer_holds_lock.set()
            await release_newer.wait()
            return decision.id

    async def attempt_stale_publication() -> None:
        await newer_holds_lock.wait()
        async with sessions.begin() as session:
            await stale_outcome.publisher(session, None)

    newer_task = asyncio.create_task(publish_newer_answer())
    await newer_holds_lock.wait()
    stale_task = asyncio.create_task(attempt_stale_publication())
    await asyncio.sleep(0.05)
    assert not stale_task.done()
    release_newer.set()
    newer_decision_id, _ = await asyncio.gather(newer_task, stale_task)

    async with sessions() as session:
        decisions = list((await session.scalars(select(EligibilityDecision))).all())
        assert len(decisions) == 1
        assert decisions[0].id == newer_decision_id
        assert decisions[0].is_current is True
        assert decisions[0].published_verdict == "INELIGIBLE"
    await engine.dispose()
