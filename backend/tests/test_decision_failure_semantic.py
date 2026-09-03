from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app.jobs import enqueue_job
from app.models import (
    AIStageRun,
    AnalysisRun,
    Announcement,
    AnnouncementVersion,
    CompanyProfile,
    CompanyProfileVersion,
    EligibilityDecision,
    ExtractedCondition,
    Job,
    User,
)
from app.pipeline.handlers import build_handler_registry
from app.pipeline.jobs import JobQueue
from app.pipeline.worker import Worker


async def _isolation_ok() -> bool:
    return True


def _semantic_ir() -> dict:
    return {
        "analysis_version": "analysis-v1",
        "summary": "바이오 기업 대상",
        "tracks": [{"track_id": "general", "label": "일반"}],
        "roles": [],
        "groups": [
            {
                "group_id": "root",
                "parent_group_id": None,
                "operator": "ALL",
                "track_ids": ["general"],
                "role_keys": [],
            }
        ],
        "conditions": [
            {
                "condition_id": "semantic-fit",
                "group_id": "root",
                "kind": "MANDATORY",
                "subject": "PRIMARY_INDUSTRY",
                "operator": "SEMANTIC_MATCH",
                "expected_value": {"type": "STRING", "value": "바이오"},
                "unit": None,
                "reference_date": None,
                "evidence": [],
            }
        ],
        "questions": [],
    }


async def _seed_semantic_analysis(session_factory) -> dict[str, str]:
    async with session_factory.begin() as db:
        user = await db.scalar(select(User).where(User.username == "demo.user"))
        profile = CompanyProfile(user_id=user.id)
        db.add(profile)
        await db.flush()
        profile_version = CompanyProfileVersion(
            profile_id=profile.id,
            user_id=user.id,
            version=1,
            snapshot={"companyName": "합성 기업", "primaryIndustry": "의료용 바이오 센서"},
            raw_input={"companyName": "합성 기업", "primaryIndustry": "의료용 바이오 센서"},
        )
        db.add(profile_version)
        await db.flush()
        profile.current_version_id = profile_version.id

        announcement = Announcement(
            source_id="SEMANTIC-1",
            source_url="https://example.test/semantic",
            source_available=True,
        )
        db.add(announcement)
        await db.flush()
        version = AnnouncementVersion(
            announcement_id=announcement.id,
            raw_payload={"jsonArray": []},
            content_hash="s" * 64,
            title="의미판단 공고",
            body_text="바이오 기업을 지원합니다.",
        )
        db.add(version)
        await db.flush()
        announcement.current_version_id = version.id
        now = datetime.now(UTC)
        analysis = AnalysisRun(
            announcement_version_id=version.id,
            status="SUCCEEDED",
            analysis_version="analysis-v1",
            canonical_ir=_semantic_ir(),
            started_at=now,
            completed_at=now,
        )
        db.add(analysis)
        await db.flush()
        db.add(
            ExtractedCondition(
                analysis_run_id=analysis.id,
                condition_key="semantic-fit",
                group_key="root",
                track_key="general",
                role_key=None,
                kind="MANDATORY",
                subject="PRIMARY_INDUSTRY",
                operator="SEMANTIC_MATCH",
                expected_value={"type": "STRING", "value": "바이오"},
                unit=None,
                reference_date=None,
                evidence=[],
            )
        )
        db.add(
            AIStageRun(
                analysis_run_id=analysis.id,
                company_profile_version_id=profile_version.id,
                stage="SEMANTIC_JUDGMENT",
                model="gpt-5.6-terra",
                effort="high",
                prompt_version="semantic-judgment-v1.prompt",
                schema_version="semantic-judgment-v1.schema",
                input_hash="a" * 64,
                structured_output={
                    "condition_id": "semantic-fit",
                    "status": "PASS",
                    "explanation": "업종 의미가 일치합니다.",
                },
                evidence=[],
                duration_ms=1,
                attempt=1,
            )
        )
        return {
            "user": user.id,
            "profile": profile.id,
            "profile_version": profile_version.id,
            "announcement": announcement.id,
            "version": version.id,
        }


def _worker(session_factory, tmp_path: Path, *, worker_id: str) -> Worker:
    return Worker(
        worker_id=worker_id,
        queue=JobQueue(session_factory),
        handlers=build_handler_registry(
            sessions=session_factory,
            fixture_root=tmp_path,
            source_storage_root=tmp_path / "sources",
        ),
        isolation_check=_isolation_ok,
    )


def test_role_and_answer_reevaluation_reuses_exact_profile_semantic_result(
    session_factory, tmp_path: Path
) -> None:
    async def scenario() -> None:
        seeded = await _seed_semantic_analysis(session_factory)
        for cause in ("ROLE_SELECTED", "ANSWER_SAVED"):
            async with session_factory.begin() as db:
                job = await enqueue_job(
                    db,
                    "DECISION_REEVALUATE",
                    {
                        "userId": seeded["user"],
                        "announcementId": seeded["announcement"],
                        "announcementVersionId": seeded["version"],
                        "companyProfileVersionId": seeded["profile_version"],
                        "cause": cause,
                    },
                )
            assert await _worker(session_factory, tmp_path, worker_id=cause).run_once() == 1
            async with session_factory() as db:
                completed = await db.get(Job, job.id)
                current = await db.scalar(
                    select(EligibilityDecision).where(EligibilityDecision.is_current.is_(True))
                )
                assert completed.status == "SUCCEEDED"
                assert current.company_profile_version_id == seeded["profile_version"]
                assert current.calculated_verdict == "ELIGIBLE"
                assert current.published_verdict == "ELIGIBLE"

    asyncio.run(scenario())


def test_company_change_without_matching_semantic_result_queues_reanalysis_without_regression(
    session_factory, tmp_path: Path
) -> None:
    async def scenario() -> None:
        seeded = await _seed_semantic_analysis(session_factory)
        async with session_factory.begin() as db:
            old = EligibilityDecision(
                user_id=seeded["user"],
                announcement_id=seeded["announcement"],
                announcement_version_id=seeded["version"],
                company_profile_version_id=seeded["profile_version"],
                calculated_verdict="ELIGIBLE",
                published_verdict="ELIGIBLE",
                decision_origin="CALCULATED",
                is_current=True,
            )
            db.add(old)
            profile = await db.get(CompanyProfile, seeded["profile"])
            next_profile = CompanyProfileVersion(
                profile_id=profile.id,
                user_id=seeded["user"],
                version=2,
                snapshot={"companyName": "변경 기업", "primaryIndustry": "바이오 제조"},
                raw_input={"companyName": "변경 기업", "primaryIndustry": "바이오 제조"},
            )
            db.add(next_profile)
            await db.flush()
            profile.current_version_id = next_profile.id
            job = await enqueue_job(
                db,
                "DECISION_REEVALUATE",
                {
                    "userId": seeded["user"],
                    "announcementId": seeded["announcement"],
                    "announcementVersionId": seeded["version"],
                    "companyProfileVersionId": next_profile.id,
                    "cause": "COMPANY_PROFILE_CHANGED",
                },
            )
            next_profile_id = next_profile.id

        assert await _worker(session_factory, tmp_path, worker_id="profile-change").run_once() == 1
        async with session_factory() as db:
            completed = await db.get(Job, job.id)
            current = await db.scalar(
                select(EligibilityDecision).where(EligibilityDecision.is_current.is_(True))
            )
            analyze = await db.scalar(
                select(Job).where(
                    Job.job_type == "ANNOUNCEMENT_ANALYZE",
                    Job.status == "QUEUED",
                )
            )
            assert completed.status == "SUCCEEDED"
            assert current.company_profile_version_id == seeded["profile_version"]
            assert current.published_verdict == "ELIGIBLE"
            assert analyze.payload["companyProfileVersionId"] == next_profile_id

    asyncio.run(scenario())


def test_final_analysis_failure_atomically_replaces_current_decision_with_safe_result(
    session_factory, tmp_path: Path
) -> None:
    async def scenario() -> None:
        seeded = await _seed_semantic_analysis(session_factory)
        async with session_factory.begin() as db:
            old = EligibilityDecision(
                user_id=seeded["user"],
                announcement_id=seeded["announcement"],
                announcement_version_id=seeded["version"],
                company_profile_version_id=seeded["profile_version"],
                calculated_verdict="ELIGIBLE",
                published_verdict="ELIGIBLE",
                decision_origin="CALCULATED",
                is_current=True,
            )
            db.add(old)
            job = await enqueue_job(
                db,
                "ANNOUNCEMENT_ANALYZE",
                {
                    "announcementId": seeded["announcement"],
                    "announcementVersionId": seeded["version"],
                },
            )

        assert (
            await _worker(session_factory, tmp_path, worker_id="analysis-failure").run_once() == 1
        )
        async with session_factory() as db:
            failed_job = await db.get(Job, job.id)
            decisions = list(
                (
                    await db.scalars(
                        select(EligibilityDecision).order_by(EligibilityDecision.created_at)
                    )
                ).all()
            )
            failed_analysis = await db.scalar(
                select(AnalysisRun).where(AnalysisRun.status == "FAILED_FINAL")
            )
            assert failed_job.status == "FAILED_FINAL"
            assert failed_job.error_code == "AI_EXECUTOR_UNAVAILABLE"
            assert len(decisions) == 2
            assert decisions[0].is_current is False
            assert decisions[1].is_current is True
            assert decisions[1].announcement_version_id == seeded["version"]
            assert decisions[1].company_profile_version_id == seeded["profile_version"]
            assert decisions[1].calculated_verdict is None
            assert decisions[1].published_verdict == "NEEDS_CONFIRMATION"
            assert decisions[1].decision_origin == "SYSTEM_FAILURE"
            assert failed_analysis.error_code == "AI_EXECUTOR_UNAVAILABLE"

    asyncio.run(scenario())
