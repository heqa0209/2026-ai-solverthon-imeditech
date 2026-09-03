from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from sqlalchemy import func, select
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.jobs import enqueue_job
from app.models import (
    AnalysisRun,
    Announcement,
    CompanyProfile,
    CompanyProfileVersion,
    ConditionResult,
    EligibilityDecision,
    ExtractedCondition,
    Job,
    SourceFile,
    User,
)
from app.pipeline.fixtures import load_fixture_manifest
from app.pipeline.handlers import build_handler_registry
from app.pipeline.jobs import JobQueue
from app.pipeline.persistence import persist_demo_fixture
from app.pipeline.service import DatabasePipelineCLIService
from app.pipeline.worker import Worker

FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "demo"
MANIFEST = FIXTURE_ROOT / "manifest.json"


async def _profile(session_factory, *, scale: str = "SMALL") -> CompanyProfileVersion:
    async with session_factory() as db:
        user = await db.scalar(select(User).where(User.username == "demo.user"))
        profile = await db.scalar(select(CompanyProfile).where(CompanyProfile.user_id == user.id))
        if profile is None:
            profile = CompanyProfile(user_id=user.id)
            db.add(profile)
            await db.flush()
            version_number = 1
        else:
            version_number = await db.scalar(
                select(func.count(CompanyProfileVersion.id)).where(
                    CompanyProfileVersion.profile_id == profile.id
                )
            )
            version_number = int(version_number or 0) + 1
        version = CompanyProfileVersion(
            profile_id=profile.id,
            user_id=user.id,
            version=version_number,
            snapshot={"companyName": "합성 테스트 기업", "companyScale": scale},
            raw_input={"companyName": "합성 테스트 기업", "companyScale": scale},
        )
        db.add(version)
        await db.flush()
        profile.current_version_id = version.id
        await db.commit()
        return version


async def _isolation_ok() -> bool:
    return True


def test_demo_manifest_freezes_required_extraction_coverage_matrix() -> None:
    manifest = load_fixture_manifest(MANIFEST)
    assert manifest.expected_ai_stages == ["CONDITION_EXTRACTION", "USER_EXPLANATION"]
    assert set(manifest.coverage_matrix.model_dump()) == {
        "native_hwpx",
        "native_pdf",
        "mixed_pdf",
        "vision_ocr",
        "limit_exceeded",
    }


def test_fixture_loader_persists_traceable_inputs_and_publishes_decision(
    session_factory, tmp_path: Path
) -> None:
    async def scenario() -> None:
        profile_version = await _profile(session_factory)
        async with session_factory.begin() as db:
            first = await persist_demo_fixture(
                db, MANIFEST, source_storage_root=tmp_path / "sources"
            )
        assert first.processed == 1
        assert first.version_created is True
        assert first.analysis_created is True
        assert first.decisions_published == 1

        async with session_factory.begin() as db:
            second = await persist_demo_fixture(
                db, MANIFEST, source_storage_root=tmp_path / "sources"
            )
        assert second.processed == 0

        async with session_factory() as db:
            announcement = await db.get(Announcement, first.announcement_id)
            analysis = await db.get(AnalysisRun, first.analysis_run_id)
            source = await db.get(SourceFile, "demo-2026-001-body")
            condition = await db.scalar(
                select(ExtractedCondition).where(ExtractedCondition.analysis_run_id == analysis.id)
            )
            decision = await db.scalar(
                select(EligibilityDecision).where(
                    EligibilityDecision.user_id == profile_version.user_id,
                    EligibilityDecision.is_current.is_(True),
                )
            )
            result = await db.scalar(
                select(ConditionResult).where(ConditionResult.decision_id == decision.id)
            )
            assert announcement.current_version_id == first.announcement_version_id
            assert analysis.status == "SUCCEEDED"
            assert source.extracted_text and "지원 대상은 소기업" in source.extracted_text
            assert condition.evidence[0]["source_file_id"] == source.id
            assert decision.published_verdict == "ELIGIBLE"
            assert result.status == "PASS"
            assert decision.company_profile_version_id == profile_version.id

    asyncio.run(scenario())


def test_fixture_source_is_stored_and_downloadable_with_matching_hash(
    authenticated_client, session_factory, settings
) -> None:
    client, csrf = authenticated_client
    saved = client.put(
        "/api/v1/company",
        json={"companyName": "합성 테스트 기업", "companyScale": "SMALL"},
        headers={"Origin": "http://testserver", "X-CSRF-Token": csrf, "If-Match": '"0"'},
    )
    assert saved.status_code == 200

    async def load() -> str:
        async with session_factory.begin() as db:
            result = await persist_demo_fixture(
                db, MANIFEST, source_storage_root=settings.source_storage_root
            )
            return result.announcement_id

    announcement_id = asyncio.run(load())
    response = client.get(f"/api/v1/announcements/{announcement_id}/files/demo-2026-001-body")
    assert response.status_code == 200
    assert hashlib.sha256(response.content).hexdigest() == (
        "3f29f2c56d063859a75b63829e526d457c4b1cc1bb370bbec3c191dca5218d40"
    )


def test_decision_job_handler_publishes_new_profile_version(
    session_factory, tmp_path: Path
) -> None:
    async def scenario() -> None:
        await _profile(session_factory, scale="SMALL")
        async with session_factory.begin() as db:
            loaded = await persist_demo_fixture(
                db, MANIFEST, source_storage_root=tmp_path / "sources"
            )
        large_profile = await _profile(session_factory, scale="LARGE")
        async with session_factory() as db:
            user = await db.get(User, large_profile.user_id)
            job = await enqueue_job(
                db,
                "DECISION_REEVALUATE",
                {
                    "userId": user.id,
                    "announcementId": loaded.announcement_id,
                    "announcementVersionId": loaded.announcement_version_id,
                    "companyProfileVersionId": large_profile.id,
                    "cause": "TEST",
                },
            )
            await db.commit()
            job_id = job.id
        worker = Worker(
            worker_id="integration-worker",
            queue=JobQueue(session_factory),
            handlers=build_handler_registry(
                sessions=session_factory,
                fixture_root=FIXTURE_ROOT,
                source_storage_root=tmp_path / "sources",
            ),
            isolation_check=_isolation_ok,
        )
        assert await worker.run_once() == 1
        async with session_factory() as db:
            completed = await db.get(Job, job_id)
            decision = await db.scalar(
                select(EligibilityDecision).where(
                    EligibilityDecision.user_id == large_profile.user_id,
                    EligibilityDecision.is_current.is_(True),
                )
            )
            assert completed.status == "SUCCEEDED"
            assert decision.company_profile_version_id == large_profile.id
            assert decision.published_verdict == "INELIGIBLE"

    asyncio.run(scenario())


def test_analysis_handler_supports_fixture_and_fails_without_real_executor(
    session_factory, tmp_path: Path
) -> None:
    async def scenario() -> None:
        async with session_factory() as db:
            fixture_job = await enqueue_job(
                db,
                "ANNOUNCEMENT_ANALYZE",
                {"fixtureManifest": "manifest.json"},
            )
            await db.commit()
            fixture_job_id = fixture_job.id
        worker = Worker(
            worker_id="analysis-worker",
            queue=JobQueue(session_factory),
            handlers=build_handler_registry(
                sessions=session_factory,
                fixture_root=FIXTURE_ROOT,
                source_storage_root=tmp_path / "sources",
            ),
            isolation_check=_isolation_ok,
        )
        assert await worker.run_once() == 1
        async with session_factory() as db:
            completed = await db.get(Job, fixture_job_id)
            assert completed.status == "SUCCEEDED"
            announcement = await db.scalar(
                select(Announcement).where(Announcement.source_id == "DEMO-2026-001")
            )
            missing_executor = await enqueue_job(
                db,
                "ANNOUNCEMENT_ANALYZE",
                {
                    "announcementId": announcement.id,
                    "announcementVersionId": announcement.current_version_id,
                },
            )
            await db.commit()
            missing_executor_id = missing_executor.id
        assert await worker.run_once() == 1
        async with session_factory() as db:
            failed = await db.get(Job, missing_executor_id)
            assert failed.status == "FAILED_FINAL"
            assert failed.error_code == "AI_EXECUTOR_UNAVAILABLE"

    asyncio.run(scenario())


def test_database_cli_service_loads_fixture_and_central_cli_is_wired(
    session_factory, tmp_path: Path
) -> None:
    async def load() -> int:
        await _profile(session_factory)
        service = DatabasePipelineCLIService(
            session_factory,
            fixture_root=FIXTURE_ROOT,
            source_storage_root=tmp_path / "sources",
        )
        return await service.load_fixture(MANIFEST)

    assert asyncio.run(load()) == 1
    help_result = CliRunner().invoke(cli_app, ["--help"])
    assert help_result.exit_code == 0, help_result.output
    for command in ("collect", "announcement", "decision", "job", "fixture", "acceptance"):
        assert command in help_result.output


def test_database_cli_service_enqueues_scoped_decision_job(session_factory, tmp_path: Path) -> None:
    async def scenario() -> None:
        await _profile(session_factory)
        service = DatabasePipelineCLIService(
            session_factory,
            fixture_root=FIXTURE_ROOT,
            source_storage_root=tmp_path / "sources",
        )
        assert await service.load_fixture(MANIFEST) == 1
        async with session_factory() as db:
            announcement = await db.scalar(
                select(Announcement).where(Announcement.source_id == "DEMO-2026-001")
            )
        preview = await service.preview("decision.reevaluate", announcement.id)
        assert preview.expected_count == 1
        assert preview.target_version == announcement.current_version_id
        assert await service.execute("decision.reevaluate", announcement.id) == 1
        async with session_factory() as db:
            job = await db.scalar(select(Job).where(Job.job_type == "DECISION_REEVALUATE"))
        assert job is not None
        assert "status=QUEUED" in await service.job_status(job.id)

    asyncio.run(scenario())
