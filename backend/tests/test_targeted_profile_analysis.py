from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.jobs import enqueue_job
from app.models import (
    AIStageRun,
    AnalysisRun,
    Announcement,
    AnnouncementAnswer,
    AnnouncementRoleSelection,
    AnnouncementVersion,
    CompanyProfile,
    CompanyProfileVersion,
    ConditionResult,
    EligibilityDecision,
    ExtractedCondition,
    Job,
    SourceFile,
    User,
)
from app.pipeline.ai import AIExecutionError, AIStage, FakeAIExecutor
from app.pipeline.analyzer import ProductionAnnouncementAnalyzer
from app.pipeline.handlers import build_handler_registry
from app.pipeline.jobs import JobQueue
from app.pipeline.semantic import semantic_answer_fingerprint, semantic_input_fingerprint
from app.pipeline.worker import Worker


async def _isolation_ok() -> bool:
    return True


async def _seed_two_profiles(session_factory, storage: Path) -> dict[str, str]:
    text = "지원 대상은 소기업입니다."
    body = text.encode()
    body_hash = hashlib.sha256(body).hexdigest()
    async with session_factory.begin() as db:
        first_user = await db.scalar(select(User).where(User.username == "demo.user"))
        second_user = User(username="second.user", password_hash="test-only")
        db.add(second_user)
        await db.flush()

        versions: list[CompanyProfileVersion] = []
        for index, (user, scale) in enumerate(
            ((first_user, "SMALL"), (second_user, "LARGE")), start=1
        ):
            profile = CompanyProfile(user_id=user.id)
            db.add(profile)
            await db.flush()
            version = CompanyProfileVersion(
                profile_id=profile.id,
                user_id=user.id,
                version=1,
                snapshot={"companyName": f"합성 기업 {index}", "companyScale": scale},
                raw_input={"companyName": f"합성 기업 {index}", "companyScale": scale},
            )
            db.add(version)
            await db.flush()
            profile.current_version_id = version.id
            versions.append(version)

        announcement = Announcement(
            source_id="TARGET-PROFILE-1",
            source_url="https://example.test/target-profile",
            source_available=True,
        )
        db.add(announcement)
        await db.flush()
        announcement_version = AnnouncementVersion(
            announcement_id=announcement.id,
            raw_payload={"jsonArray": []},
            content_hash="t" * 64,
            title="대상 profile 분석 공고",
            body_text=text,
        )
        db.add(announcement_version)
        await db.flush()
        announcement.current_version_id = announcement_version.id
        relative = Path(announcement_version.id) / "body.txt"
        target = storage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        source = SourceFile(
            announcement_version_id=announcement_version.id,
            name="body.txt",
            source_url=announcement.source_url,
            storage_path=relative.as_posix(),
            sha256=body_hash,
            mime_type="text/plain",
            size_bytes=len(body),
            source_order=0,
            source_priority=10,
            download_status="SUCCEEDED",
            extraction_status="SUCCEEDED",
            extracted_text=text,
        )
        db.add(source)
        await db.flush()
        return {
            "first_user": first_user.id,
            "first_profile_version": versions[0].id,
            "second_profile_version": versions[1].id,
            "announcement": announcement.id,
            "announcement_version": announcement_version.id,
            "source": source.id,
            "source_hash": body_hash,
            "text": text,
        }


def _ir(seeded: dict[str, str]) -> dict:
    return {
        "analysis_version": "analysis-v1",
        "summary": "소기업 대상",
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
                "condition_id": "scale",
                "group_id": "root",
                "kind": "MANDATORY",
                "subject": "COMPANY_SCALE",
                "operator": "EQ",
                "expected_value": {"type": "ENUM", "value": "SMALL"},
                "unit": None,
                "reference_date": None,
                "evidence": [
                    {
                        "source_file_id": seeded["source"],
                        "source_version": seeded["source_hash"],
                        "page": None,
                        "verbatim_text": seeded["text"],
                        "source_priority": 10,
                    }
                ],
            }
        ],
        "questions": [],
    }


def _semantic_ir(seeded: dict[str, str]) -> dict:
    ir = _ir(seeded)
    ir["conditions"].append(
        {
            "condition_id": "semantic-fit",
            "group_id": "root",
            "kind": "MANDATORY",
            "subject": "OTHER",
            "operator": "SEMANTIC_MATCH",
            "expected_value": {"type": "STRING", "value": "의료기기 역량"},
            "unit": None,
            "reference_date": None,
            "evidence": ir["conditions"][0]["evidence"],
        }
    )
    return ir


def _role_semantic_ir(seeded: dict[str, str]) -> dict:
    evidence = _ir(seeded)["conditions"][0]["evidence"]
    return {
        "analysis_version": "analysis-v1",
        "summary": "주관기관은 의미판단, 참여기관은 규모조건을 확인",
        "tracks": [{"track_id": "general", "label": "일반"}],
        "roles": [
            {"role_key": "LEAD", "label": "주관기관"},
            {"role_key": "PARTNER", "label": "참여기관"},
        ],
        "groups": [
            {
                "group_id": "lead",
                "parent_group_id": None,
                "operator": "ALL",
                "track_ids": ["general"],
                "role_keys": ["LEAD"],
            },
            {
                "group_id": "partner",
                "parent_group_id": None,
                "operator": "ALL",
                "track_ids": ["general"],
                "role_keys": ["PARTNER"],
            },
        ],
        "conditions": [
            {
                "condition_id": "lead-fit",
                "group_id": "lead",
                "kind": "MANDATORY",
                "subject": "OTHER",
                "operator": "SEMANTIC_MATCH",
                "expected_value": {"type": "STRING", "value": "의료기기 역량"},
                "unit": None,
                "reference_date": None,
                "evidence": evidence,
            },
            {
                "condition_id": "partner-scale",
                "group_id": "partner",
                "kind": "MANDATORY",
                "subject": "COMPANY_SCALE",
                "operator": "EQ",
                "expected_value": {"type": "ENUM", "value": "LARGE"},
                "unit": None,
                "reference_date": None,
                "evidence": evidence,
            },
        ],
        "questions": [],
    }


def test_targeted_analysis_transmits_and_publishes_only_requested_current_profile(
    session_factory, tmp_path: Path
) -> None:
    async def scenario() -> None:
        storage = tmp_path / "sources"
        seeded = await _seed_two_profiles(session_factory, storage)
        fake = FakeAIExecutor(
            {
                AIStage.CONDITION_EXTRACTION: _ir(seeded),
                AIStage.USER_EXPLANATION: {"explanation": "소기업 조건을 충족합니다."},
            }
        )
        analyzer = ProductionAnnouncementAnalyzer(
            sessions=session_factory,
            executor=fake,
            source_storage_root=storage,
        )
        publisher = await analyzer.prepare(
            type(
                "Payload",
                (),
                {
                    "announcement_id": seeded["announcement"],
                    "announcement_version_id": seeded["announcement_version"],
                    "company_profile_version_id": seeded["first_profile_version"],
                },
            )(),
            type("Context", (), {})(),
        )
        async with session_factory.begin() as db:
            await publisher(db, type("Job", (), {})())

        async with session_factory() as db:
            decisions = list((await db.scalars(select(EligibilityDecision))).all())
            scoped_stages = list(
                (
                    await db.scalars(
                        select(AIStageRun).where(AIStageRun.company_profile_version_id.is_not(None))
                    )
                ).all()
            )
            assert len(decisions) == 1
            assert decisions[0].user_id == seeded["first_user"]
            assert decisions[0].company_profile_version_id == seeded["first_profile_version"]
            assert decisions[0].published_verdict == "ELIGIBLE"
            assert {stage.company_profile_version_id for stage in scoped_stages} == {
                seeded["first_profile_version"]
            }
            explanation_call = next(
                item for item in fake.invocations if item.stage == AIStage.USER_EXPLANATION
            )
            transmitted = explanation_call.stdin.decode()
            assert seeded["first_profile_version"] in transmitted
            assert seeded["second_profile_version"] not in transmitted
            assert "합성 기업 1" in transmitted
            assert "합성 기업 2" not in transmitted

    asyncio.run(scenario())


def test_targeted_analysis_rejects_non_current_profile_without_model_call(
    session_factory, tmp_path: Path
) -> None:
    async def scenario() -> None:
        storage = tmp_path / "sources"
        seeded = await _seed_two_profiles(session_factory, storage)
        async with session_factory.begin() as db:
            old_version = await db.get(CompanyProfileVersion, seeded["first_profile_version"])
            profile = await db.get(CompanyProfile, old_version.profile_id)
            current = CompanyProfileVersion(
                profile_id=profile.id,
                user_id=old_version.user_id,
                version=2,
                snapshot={"companyName": "최신 기업", "companyScale": "SMALL"},
                raw_input={"companyName": "최신 기업", "companyScale": "SMALL"},
            )
            db.add(current)
            await db.flush()
            profile.current_version_id = current.id

        fake = FakeAIExecutor({})
        analyzer = ProductionAnnouncementAnalyzer(
            sessions=session_factory,
            executor=fake,
            source_storage_root=storage,
        )
        with pytest.raises(AIExecutionError) as caught:
            await analyzer.prepare(
                type(
                    "Payload",
                    (),
                    {
                        "announcement_id": seeded["announcement"],
                        "announcement_version_id": seeded["announcement_version"],
                        "company_profile_version_id": seeded["first_profile_version"],
                    },
                )(),
                type("Context", (), {})(),
            )
        assert caught.value.code == "ANALYSIS_PROFILE_VERSION_INVALID"
        assert fake.invocations == []

    asyncio.run(scenario())


def test_targeted_reanalysis_includes_latest_non_boolean_answer_and_reuses_exact_result(
    session_factory, tmp_path: Path
) -> None:
    async def scenario() -> None:
        storage = tmp_path / "sources"
        seeded = await _seed_two_profiles(session_factory, storage)
        semantic_ir = _semantic_ir(seeded)
        async with session_factory.begin() as db:
            previous = AnalysisRun(
                announcement_version_id=seeded["announcement_version"],
                status="SUCCEEDED",
                analysis_version="previous-v1",
                canonical_ir=semantic_ir,
            )
            db.add(previous)
            await db.flush()
            previous_condition = ExtractedCondition(
                analysis_run_id=previous.id,
                condition_key="semantic-fit",
                group_key="root",
                track_key="general",
                role_key=None,
                kind="MANDATORY",
                subject="OTHER",
                operator="SEMANTIC_MATCH",
                expected_value={"type": "STRING", "value": "의료기기 역량"},
                unit=None,
                reference_date=None,
                evidence=semantic_ir["conditions"][1]["evidence"],
            )
            db.add(previous_condition)
            await db.flush()
            db.add(
                AnnouncementAnswer(
                    user_id=seeded["first_user"],
                    announcement_version_id=seeded["announcement_version"],
                    condition_id=previous_condition.id,
                    value="정밀 로봇 의료기기",
                    source="USER_VERIFIED",
                    memo="담당자 확인",
                )
            )

        fake = FakeAIExecutor(
            {
                AIStage.CONDITION_EXTRACTION: semantic_ir,
                AIStage.SEMANTIC_JUDGMENT: {
                    "status": "PASS",
                    "explanation": "최신 답변을 포함해 역량이 일치합니다.",
                },
                AIStage.FINAL_AI_VALIDATION: {
                    "result": "ACCEPT",
                    "corrections": [],
                    "reason": "근거 확인",
                },
                AIStage.USER_EXPLANATION: {"explanation": "두 필수조건을 충족합니다."},
            }
        )
        analyzer = ProductionAnnouncementAnalyzer(
            sessions=session_factory,
            executor=fake,
            source_storage_root=storage,
        )
        publisher = await analyzer.prepare(
            type(
                "Payload",
                (),
                {
                    "announcement_id": seeded["announcement"],
                    "announcement_version_id": seeded["announcement_version"],
                    "company_profile_version_id": seeded["first_profile_version"],
                },
            )(),
            type("Context", (), {})(),
        )
        async with session_factory.begin() as db:
            await publisher(db, type("Job", (), {})())

        semantic_call = next(
            item for item in fake.invocations if item.stage == AIStage.SEMANTIC_JUDGMENT
        )
        transmitted = semantic_call.stdin.decode()
        assert '"value":"정밀 로봇 의료기기"' in transmitted
        assert '"source":"USER_VERIFIED"' in transmitted
        assert '"memo":"담당자 확인"' in transmitted
        final_call = next(
            item for item in fake.invocations if item.stage == AIStage.FINAL_AI_VALIDATION
        )
        assert '"latest_answers":{"semantic-fit"' in final_call.stdin.decode()
        assert '"value":"정밀 로봇 의료기기"' in final_call.stdin.decode()

        async with session_factory.begin() as db:
            semantic_stage = await db.scalar(
                select(AIStageRun).where(AIStageRun.stage == "SEMANTIC_JUDGMENT")
            )
            assert semantic_stage.structured_output["answer_fingerprint"]
            reevaluate = await enqueue_job(
                db,
                "DECISION_REEVALUATE",
                {
                    "userId": seeded["first_user"],
                    "announcementId": seeded["announcement"],
                    "announcementVersionId": seeded["announcement_version"],
                    "companyProfileVersionId": seeded["first_profile_version"],
                    "cause": "USER_REQUESTED",
                },
            )

        worker = Worker(
            worker_id="answer-reuse",
            queue=JobQueue(session_factory),
            handlers=build_handler_registry(
                sessions=session_factory,
                fixture_root=tmp_path,
                source_storage_root=storage,
            ),
            isolation_check=_isolation_ok,
        )
        assert await worker.run_once() == 1
        async with session_factory() as db:
            completed = await db.get(Job, reevaluate.id)
            current = await db.scalar(
                select(EligibilityDecision).where(EligibilityDecision.is_current.is_(True))
            )
            queued_analysis = await db.scalar(
                select(Job).where(
                    Job.job_type == "ANNOUNCEMENT_ANALYZE",
                    Job.status == "QUEUED",
                )
            )
            assert completed.status == "SUCCEEDED"
            assert current.published_verdict == "ELIGIBLE"
            assert queued_analysis is None

    asyncio.run(scenario())


def test_targeted_semantic_reanalysis_preserves_the_users_selected_role(
    session_factory, tmp_path: Path
) -> None:
    async def scenario() -> None:
        storage = tmp_path / "sources"
        seeded = await _seed_two_profiles(session_factory, storage)
        role_ir = _role_semantic_ir(seeded)
        async with session_factory.begin() as db:
            db.add(
                AnnouncementRoleSelection(
                    user_id=seeded["first_user"],
                    announcement_id=seeded["announcement"],
                    announcement_version_id=seeded["announcement_version"],
                    role_key="LEAD",
                )
            )

        fake = FakeAIExecutor(
            {
                AIStage.CONDITION_EXTRACTION: role_ir,
                AIStage.SEMANTIC_JUDGMENT: {
                    "status": "PASS",
                    "explanation": "주관기관 역량이 일치합니다.",
                },
                AIStage.FINAL_AI_VALIDATION: {
                    "result": "ACCEPT",
                    "corrections": [],
                    "reason": "근거 확인",
                },
                AIStage.USER_EXPLANATION: {"explanation": "주관기관 조건을 충족합니다."},
            }
        )
        analyzer = ProductionAnnouncementAnalyzer(
            sessions=session_factory,
            executor=fake,
            source_storage_root=storage,
        )
        publisher = await analyzer.prepare(
            type(
                "Payload",
                (),
                {
                    "announcement_id": seeded["announcement"],
                    "announcement_version_id": seeded["announcement_version"],
                    "company_profile_version_id": seeded["first_profile_version"],
                },
            )(),
            type("Context", (), {})(),
        )
        async with session_factory.begin() as db:
            await publisher(db, type("Job", (), {})())

        semantic_call = next(
            item for item in fake.invocations if item.stage == AIStage.SEMANTIC_JUDGMENT
        )
        assert '"selected_role_key":"LEAD"' in semantic_call.stdin.decode()
        async with session_factory() as db:
            decision = await db.scalar(
                select(EligibilityDecision).where(EligibilityDecision.is_current.is_(True))
            )
            result_rows = (
                await db.execute(
                    select(ExtractedCondition.condition_key, ConditionResult.status)
                    .join(
                        ConditionResult,
                        ConditionResult.condition_id == ExtractedCondition.id,
                    )
                    .where(ConditionResult.decision_id == decision.id)
                )
            ).all()
            assert decision.selected_role_key == "LEAD"
            assert decision.published_verdict == "ELIGIBLE"
            assert dict(result_rows) == {
                "lead-fit": "PASS",
                "partner-scale": "NOT_APPLICABLE",
            }

    asyncio.run(scenario())


def test_stale_semantic_input_is_superseded_before_model_execution(
    session_factory, tmp_path: Path
) -> None:
    async def scenario() -> None:
        storage = tmp_path / "sources"
        seeded = await _seed_two_profiles(session_factory, storage)
        semantic_ir = _semantic_ir(seeded)
        answer_a = {"value": {"detail": "답변 A"}, "source": "USER_VERIFIED", "memo": None}
        async with session_factory.begin() as db:
            base = AnalysisRun(
                announcement_version_id=seeded["announcement_version"],
                status="SUCCEEDED",
                analysis_version="base-v1",
                canonical_ir=semantic_ir,
                completed_at=datetime.now(UTC),
            )
            db.add(base)
            await db.flush()
            condition = ExtractedCondition(
                analysis_run_id=base.id,
                condition_key="semantic-fit",
                group_key="root",
                track_key="general",
                role_key=None,
                kind="MANDATORY",
                subject="OTHER",
                operator="SEMANTIC_MATCH",
                expected_value={"type": "STRING", "value": "의료기기 역량"},
                unit=None,
                reference_date=None,
                evidence=semantic_ir["conditions"][1]["evidence"],
            )
            db.add(condition)
            await db.flush()
            db.add(
                AnnouncementAnswer(
                    user_id=seeded["first_user"],
                    announcement_version_id=seeded["announcement_version"],
                    condition_id=condition.id,
                    **answer_a,
                )
            )
            await db.flush()
            input_hash = semantic_input_fingerprint(
                analysis_run_id=base.id,
                answer_fingerprints={"semantic-fit": semantic_answer_fingerprint(answer_a)},
                selected_role_key=None,
            )
            base_id = base.id
            db.add(
                AnnouncementAnswer(
                    user_id=seeded["first_user"],
                    announcement_version_id=seeded["announcement_version"],
                    condition_id=condition.id,
                    value={"detail": "답변 B"},
                    source="USER_VERIFIED",
                    memo=None,
                )
            )

        fake = FakeAIExecutor({})
        analyzer = ProductionAnnouncementAnalyzer(
            sessions=session_factory,
            executor=fake,
            source_storage_root=storage,
        )
        publisher = await analyzer.prepare(
            type(
                "Payload",
                (),
                {
                    "announcement_id": seeded["announcement"],
                    "announcement_version_id": seeded["announcement_version"],
                    "company_profile_version_id": seeded["first_profile_version"],
                    "semantic_input_hash": input_hash,
                    "semantic_base_analysis_run_id": base_id,
                },
            )(),
            type("Context", (), {})(),
        )
        assert fake.invocations == []
        async with session_factory.begin() as db:
            await publisher(db, type("Job", (), {})())
        async with session_factory() as db:
            assert len(list((await db.scalars(select(AnalysisRun))).all())) == 1
            assert await db.scalar(select(EligibilityDecision)) is None
            assert await db.scalar(select(AIStageRun)) is None

    asyncio.run(scenario())


def test_answer_change_during_semantic_analysis_prevents_database_publication(
    session_factory, tmp_path: Path
) -> None:
    async def scenario() -> None:
        storage = tmp_path / "sources"
        seeded = await _seed_two_profiles(session_factory, storage)
        semantic_ir = _semantic_ir(seeded)
        answer_a = {"value": {"detail": "답변 A"}, "source": "USER_VERIFIED", "memo": None}
        async with session_factory.begin() as db:
            base = AnalysisRun(
                announcement_version_id=seeded["announcement_version"],
                status="SUCCEEDED",
                analysis_version="base-v1",
                canonical_ir=semantic_ir,
                completed_at=datetime.now(UTC),
            )
            db.add(base)
            await db.flush()
            condition = ExtractedCondition(
                analysis_run_id=base.id,
                condition_key="semantic-fit",
                group_key="root",
                track_key="general",
                role_key=None,
                kind="MANDATORY",
                subject="OTHER",
                operator="SEMANTIC_MATCH",
                expected_value={"type": "STRING", "value": "의료기기 역량"},
                unit=None,
                reference_date=None,
                evidence=semantic_ir["conditions"][1]["evidence"],
            )
            db.add(condition)
            await db.flush()
            db.add(
                AnnouncementAnswer(
                    user_id=seeded["first_user"],
                    announcement_version_id=seeded["announcement_version"],
                    condition_id=condition.id,
                    **answer_a,
                )
            )
            await db.flush()
            input_hash = semantic_input_fingerprint(
                analysis_run_id=base.id,
                answer_fingerprints={"semantic-fit": semantic_answer_fingerprint(answer_a)},
                selected_role_key=None,
            )
            base_id = base.id
            condition_id = condition.id

        fake = FakeAIExecutor(
            {
                AIStage.CONDITION_EXTRACTION: semantic_ir,
                AIStage.SEMANTIC_JUDGMENT: {
                    "status": "PASS",
                    "explanation": "답변 A 기준 결과",
                },
                AIStage.FINAL_AI_VALIDATION: {
                    "result": "ACCEPT",
                    "corrections": [],
                    "reason": "근거 확인",
                },
                AIStage.USER_EXPLANATION: {"explanation": "답변 A 기준 설명"},
            }
        )
        analyzer = ProductionAnnouncementAnalyzer(
            sessions=session_factory,
            executor=fake,
            source_storage_root=storage,
        )
        publisher = await analyzer.prepare(
            type(
                "Payload",
                (),
                {
                    "announcement_id": seeded["announcement"],
                    "announcement_version_id": seeded["announcement_version"],
                    "company_profile_version_id": seeded["first_profile_version"],
                    "semantic_input_hash": input_hash,
                    "semantic_base_analysis_run_id": base_id,
                },
            )(),
            type("Context", (), {})(),
        )
        assert any(item.stage == AIStage.SEMANTIC_JUDGMENT for item in fake.invocations)
        async with session_factory.begin() as db:
            db.add(
                AnnouncementAnswer(
                    user_id=seeded["first_user"],
                    announcement_version_id=seeded["announcement_version"],
                    condition_id=condition_id,
                    value={"detail": "답변 B"},
                    source="USER_VERIFIED",
                    memo=None,
                )
            )
        async with session_factory.begin() as db:
            await publisher(db, type("Job", (), {})())
        async with session_factory() as db:
            assert len(list((await db.scalars(select(AnalysisRun))).all())) == 1
            assert await db.scalar(select(EligibilityDecision)) is None
            assert await db.scalar(select(AIStageRun)) is None

    asyncio.run(scenario())


def test_general_analysis_skips_only_profile_whose_role_changed_before_publication(
    session_factory, tmp_path: Path
) -> None:
    async def scenario() -> None:
        storage = tmp_path / "sources"
        seeded = await _seed_two_profiles(session_factory, storage)
        role_ir = _role_semantic_ir(seeded)
        async with session_factory.begin() as db:
            db.add(
                AnnouncementRoleSelection(
                    user_id=seeded["first_user"],
                    announcement_id=seeded["announcement"],
                    announcement_version_id=seeded["announcement_version"],
                    role_key="LEAD",
                )
            )

        fake = FakeAIExecutor(
            {
                AIStage.CONDITION_EXTRACTION: role_ir,
                AIStage.SEMANTIC_JUDGMENT: {
                    "status": "PASS",
                    "explanation": "분석 시작 시점 역할 기준 결과",
                },
                AIStage.FINAL_AI_VALIDATION: {
                    "result": "ACCEPT",
                    "corrections": [],
                    "reason": "근거 확인",
                },
                AIStage.USER_EXPLANATION: {"explanation": "역할별 판정 설명"},
            }
        )
        analyzer = ProductionAnnouncementAnalyzer(
            sessions=session_factory,
            executor=fake,
            source_storage_root=storage,
        )
        publisher = await analyzer.prepare(
            type(
                "Payload",
                (),
                {
                    "announcement_id": seeded["announcement"],
                    "announcement_version_id": seeded["announcement_version"],
                    "company_profile_version_id": None,
                },
            )(),
            type("Context", (), {})(),
        )
        async with session_factory.begin() as db:
            db.add(
                AnnouncementRoleSelection(
                    user_id=seeded["first_user"],
                    announcement_id=seeded["announcement"],
                    announcement_version_id=seeded["announcement_version"],
                    role_key="PARTNER",
                )
            )
        async with session_factory.begin() as db:
            await publisher(db, type("Job", (), {})())

        async with session_factory() as db:
            decisions = list((await db.scalars(select(EligibilityDecision))).all())
            assert len(decisions) == 1
            assert decisions[0].user_id != seeded["first_user"]
            assert decisions[0].company_profile_version_id == seeded["second_profile_version"]

    asyncio.run(scenario())
