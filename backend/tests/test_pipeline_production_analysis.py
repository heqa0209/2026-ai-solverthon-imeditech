from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import pytest
from pypdf import PdfWriter
from sqlalchemy import select

from app.jobs import enqueue_job
from app.models import (
    AIStageRun,
    AnalysisRun,
    Announcement,
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
from app.pipeline.ai import AI_STAGE_POLICIES, AIStage, FakeAIExecutor
from app.pipeline.analyzer import ProductionAnnouncementAnalyzer, SourceInput
from app.pipeline.handlers import build_handler_registry
from app.pipeline.ir import (
    CanonicalIR,
    EvidenceSource,
    EvidenceValidationError,
    validate_evidence,
)
from app.pipeline.jobs import JobQueue
from app.pipeline.worker import Worker


async def _isolation_ok() -> bool:
    return True


def _canonical_ir(source_id: str, source_hash: str, text: str) -> dict:
    return {
        "analysis_version": "analysis-v1",
        "summary": "소기업 대상 공고",
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
                        "source_file_id": source_id,
                        "source_version": source_hash,
                        "page": None,
                        "verbatim_text": text,
                        "source_priority": 10,
                    }
                ],
            }
        ],
        "questions": [],
    }


async def _seed(
    session_factory,
    storage: Path,
    *,
    failed_attachment: bool = False,
    ocr_image: bool = False,
) -> dict[str, str]:
    text = "지원 대상은 소기업입니다."
    body = text.encode()
    body_hash = hashlib.sha256(body).hexdigest()
    async with session_factory() as db:
        user = await db.scalar(select(User).where(User.username == "demo.user"))
        profile = CompanyProfile(user_id=user.id)
        db.add(profile)
        await db.flush()
        profile_version = CompanyProfileVersion(
            profile_id=profile.id,
            user_id=user.id,
            version=1,
            snapshot={"companyName": "합성 기업", "companyScale": "SMALL"},
            raw_input={"companyName": "합성 기업", "companyScale": "SMALL"},
        )
        db.add(profile_version)
        await db.flush()
        profile.current_version_id = profile_version.id
        announcement = Announcement(
            source_id="ANALYSIS-1", source_url="https://example.test/1", source_available=True
        )
        db.add(announcement)
        await db.flush()
        version = AnnouncementVersion(
            announcement_id=announcement.id,
            raw_payload={"jsonArray": []},
            content_hash="a" * 64,
            title="분석 공고",
            body_text=text,
        )
        db.add(version)
        await db.flush()
        announcement.current_version_id = version.id
        relative = Path(version.id) / "body.txt"
        target = storage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        os.chmod(target, 0o600)
        source = SourceFile(
            id="analysis-body",
            announcement_version_id=version.id,
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
        image_hash = ""
        if ocr_image:
            image_data = b"\x89PNG\r\n\x1a\nfixture"
            image_hash = hashlib.sha256(image_data).hexdigest()
            image_relative = Path(version.id) / "scan.png"
            image_target = storage / image_relative
            image_target.write_bytes(image_data)
            os.chmod(image_target, 0o600)
            db.add(
                SourceFile(
                    id="analysis-image",
                    announcement_version_id=version.id,
                    name="scan.png",
                    source_url="https://files.test/scan.png",
                    storage_path=image_relative.as_posix(),
                    sha256=image_hash,
                    mime_type="image/png",
                    size_bytes=len(image_data),
                    source_order=1,
                    source_priority=20,
                    download_status="SUCCEEDED",
                    extraction_status="PENDING",
                )
            )
        if failed_attachment:
            db.add(
                SourceFile(
                    announcement_version_id=version.id,
                    name="missing.pdf",
                    source_url="https://files.test/missing.pdf",
                    source_order=1,
                    source_priority=20,
                    download_status="FAILED_FINAL",
                    extraction_status="SKIPPED",
                    failure_code="ATTACHMENT_DOWNLOAD_FAILED",
                )
            )
        await db.commit()
        return {
            "announcement": announcement.id,
            "version": version.id,
            "source": source.id,
            "source_hash": body_hash,
            "text": text,
            "image_hash": image_hash,
        }


def test_production_analyzer_publishes_traceable_ir_stages_conditions_and_decisions(
    session_factory, tmp_path: Path
) -> None:
    storage = tmp_path / "sources"

    async def scenario() -> None:
        seeded = await _seed(session_factory, storage)
        ir = _canonical_ir(seeded["source"], seeded["source_hash"], seeded["text"])
        fake = FakeAIExecutor(
            {
                AIStage.CONDITION_EXTRACTION: ir,
                AIStage.USER_EXPLANATION: {"explanation": "명시 조건을 충족합니다."},
            }
        )
        analyzer = ProductionAnnouncementAnalyzer(
            sessions=session_factory,
            executor=fake,
            source_storage_root=storage,
        )
        async with session_factory() as db:
            job = await enqueue_job(
                db,
                "ANNOUNCEMENT_ANALYZE",
                {
                    "announcementId": seeded["announcement"],
                    "announcementVersionId": seeded["version"],
                },
            )
            await db.commit()
            job_id = job.id
        worker = Worker(
            worker_id="analysis-worker",
            queue=JobQueue(session_factory),
            handlers=build_handler_registry(
                sessions=session_factory,
                fixture_root=tmp_path,
                source_storage_root=storage,
                analyzer=analyzer,
            ),
            isolation_check=_isolation_ok,
        )
        assert await worker.run_once() == 1
        async with session_factory() as db:
            job = await db.get(Job, job_id)
            analysis = await db.scalar(select(AnalysisRun))
            stage = await db.scalar(
                select(AIStageRun).where(AIStageRun.stage == "CONDITION_EXTRACTION")
            )
            condition = await db.scalar(select(ExtractedCondition))
            decision = await db.scalar(
                select(EligibilityDecision).where(EligibilityDecision.is_current.is_(True))
            )
            policy = AI_STAGE_POLICIES[AIStage.CONDITION_EXTRACTION]
            assert job.status == "SUCCEEDED"
            assert analysis.status == "SUCCEEDED"
            assert stage.stage == "CONDITION_EXTRACTION"
            assert (stage.model, stage.effort) == (policy.model, policy.effort)
            assert stage.structured_output == ir
            assert condition.condition_key == "scale"
            assert decision.published_verdict == "ELIGIBLE"
            assert [invocation.stage.value for invocation in fake.invocations] == [
                "CONDITION_EXTRACTION",
                "USER_EXPLANATION",
            ]

    asyncio.run(scenario())


def test_ocr_source_is_passed_as_bounded_image_and_stage_matrix_is_persisted(
    session_factory, tmp_path: Path
) -> None:
    storage = tmp_path / "sources"

    async def scenario() -> None:
        seeded = await _seed(session_factory, storage, ocr_image=True)
        ocr_text = "지원 대상은 소기업입니다."
        ir = _canonical_ir("analysis-image", seeded["image_hash"], ocr_text)
        ir["conditions"][0]["evidence"][0]["page"] = 1
        fake = FakeAIExecutor(
            {
                AIStage.OCR: {"pages": [{"page": 1, "text": ocr_text}]},
                AIStage.CONDITION_EXTRACTION: ir,
                AIStage.OCR_EVIDENCE_VALIDATION: {
                    "result": "ACCEPT",
                    "reason": "OCR 인용 확인",
                },
                AIStage.FINAL_AI_VALIDATION: {
                    "result": "ACCEPT",
                    "corrections": [],
                    "reason": "근거 확인",
                },
                AIStage.USER_EXPLANATION: {"explanation": "OCR 근거를 확인했습니다."},
            }
        )
        analyzer = ProductionAnnouncementAnalyzer(
            sessions=session_factory,
            executor=fake,
            source_storage_root=storage,
        )
        payload = type(
            "Payload",
            (),
            {
                "announcement_id": seeded["announcement"],
                "announcement_version_id": seeded["version"],
            },
        )()
        publisher = await analyzer.prepare(
            payload=payload,
            context=type("Context", (), {"job_id": "ocr-test"})(),
        )
        async with session_factory.begin() as db:
            await publisher(db, type("Job", (), {})())
        async with session_factory() as db:
            stages = list((await db.scalars(select(AIStageRun).order_by(AIStageRun.id))).all())
            assert {stage.stage for stage in stages} == {
                "OCR",
                "OCR_EVIDENCE_VALIDATION",
                "CONDITION_EXTRACTION",
                "FINAL_AI_VALIDATION",
                "USER_EXPLANATION",
            }
            expected = {
                stage.value: (policy.model, policy.effort)
                for stage, policy in AI_STAGE_POLICIES.items()
                if stage
                in {
                    AIStage.OCR,
                    AIStage.OCR_EVIDENCE_VALIDATION,
                    AIStage.CONDITION_EXTRACTION,
                    AIStage.FINAL_AI_VALIDATION,
                    AIStage.USER_EXPLANATION,
                }
            }
            assert {stage.stage: (stage.model, stage.effort) for stage in stages} == expected
            image_invocation = next(
                invocation for invocation in fake.invocations if invocation.stage == AIStage.OCR
            )
            validation_invocation = next(
                invocation
                for invocation in fake.invocations
                if invocation.stage == AIStage.OCR_EVIDENCE_VALIDATION
            )
            assert "--image" in image_invocation.args
            assert "--image" in validation_invocation.args
            assert "shell_tool" in {
                image_invocation.args[index + 1]
                for index, value in enumerate(image_invocation.args[:-1])
                if value == "--disable"
            }

    asyncio.run(scenario())


def test_multi_page_pdf_ocr_preserves_page_evidence_boundaries(
    session_factory, tmp_path: Path
) -> None:
    storage = tmp_path / "sources"
    storage.mkdir()
    pdf = storage / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    with pdf.open("wb") as output:
        writer.write(output)
    source_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    fake = FakeAIExecutor(
        {
            AIStage.OCR: {
                "pages": [
                    {"page": 1, "text": "표지입니다."},
                    {"page": 2, "text": "지원 대상은 소기업입니다."},
                ]
            }
        }
    )
    analyzer = ProductionAnnouncementAnalyzer(
        sessions=session_factory,
        executor=fake,
        source_storage_root=storage,
    )
    source = SourceInput(
        source_file_id="multi-page-pdf",
        name="scan.pdf",
        storage_path="scan.pdf",
        sha256=source_hash,
        source_priority=20,
        download_status="SUCCEEDED",
        extracted_text=None,
    )
    job_root = tmp_path / "job"
    job_root.mkdir()

    async def analyze():
        return await analyzer._analyze_source(source, job_root)

    analyzed, stage = asyncio.run(analyze())
    assert stage is not None
    assert analyzed.pages == {
        1: "표지입니다.",
        2: "지원 대상은 소기업입니다.",
    }
    assert analyzed.text == "표지입니다.\n지원 대상은 소기업입니다."
    invocation = fake.invocations[0]
    assert sum(value == "--image" for value in invocation.args) == 2

    valid_ir_data = _canonical_ir(source.source_file_id, source_hash, analyzed.pages[2])
    valid_ir_data["conditions"][0]["evidence"][0]["page"] = 2
    evidence_source = EvidenceSource(
        source_file_id=source.source_file_id,
        source_version=source_hash,
        text=analyzed.text,
        pages=analyzed.pages,
    )
    validate_evidence(CanonicalIR.model_validate(valid_ir_data), [evidence_source])

    wrong_page_data = _canonical_ir(source.source_file_id, source_hash, analyzed.pages[2])
    wrong_page_data["conditions"][0]["evidence"][0]["page"] = 1
    with pytest.raises(EvidenceValidationError):
        validate_evidence(CanonicalIR.model_validate(wrong_page_data), [evidence_source])


def test_incomplete_attachment_forces_safe_needs_confirmation(
    session_factory, tmp_path: Path
) -> None:
    storage = tmp_path / "sources"

    async def scenario() -> None:
        seeded = await _seed(session_factory, storage, failed_attachment=True)
        fake = FakeAIExecutor(
            {
                AIStage.CONDITION_EXTRACTION: _canonical_ir(
                    seeded["source"], seeded["source_hash"], seeded["text"]
                ),
                AIStage.USER_EXPLANATION: {"explanation": "명시 조건을 충족합니다."},
            }
        )
        analyzer = ProductionAnnouncementAnalyzer(
            sessions=session_factory,
            executor=fake,
            source_storage_root=storage,
        )
        publisher = await analyzer.prepare(
            payload=type(
                "Payload",
                (),
                {
                    "announcement_id": seeded["announcement"],
                    "announcement_version_id": seeded["version"],
                },
            )(),
            context=type("Context", (), {})(),
        )
        async with session_factory.begin() as db:
            await publisher(db, type("Job", (), {})())
        async with session_factory() as db:
            decision = await db.scalar(
                select(EligibilityDecision).where(EligibilityDecision.is_current.is_(True))
            )
            assert decision.calculated_verdict == "ELIGIBLE"
            assert decision.published_verdict == "NEEDS_CONFIRMATION"
            assert decision.decision_origin == "SYSTEM_FAILURE"

    asyncio.run(scenario())


def test_unresolved_ocr_evidence_becomes_unknown_without_sol_override(
    session_factory, tmp_path: Path
) -> None:
    storage = tmp_path / "sources"

    async def scenario() -> None:
        seeded = await _seed(session_factory, storage, ocr_image=True)
        ocr_text = "지원 대상은 소기업입니다."
        ir = _canonical_ir("analysis-image", seeded["image_hash"], ocr_text)
        ir["conditions"][0]["evidence"][0]["page"] = 1
        fake = FakeAIExecutor(
            {
                AIStage.OCR: {"pages": [{"page": 1, "text": ocr_text}]},
                AIStage.CONDITION_EXTRACTION: ir,
                AIStage.OCR_EVIDENCE_VALIDATION: {
                    "result": "UNRESOLVED",
                    "reason": "스캔이 흐립니다.",
                },
                AIStage.USER_EXPLANATION: {"explanation": "원문 확인이 필요합니다."},
            }
        )
        analyzer = ProductionAnnouncementAnalyzer(
            sessions=session_factory,
            executor=fake,
            source_storage_root=storage,
        )
        publisher = await analyzer.prepare(
            payload=type(
                "Payload",
                (),
                {
                    "announcement_id": seeded["announcement"],
                    "announcement_version_id": seeded["version"],
                },
            )(),
            context=type("Context", (), {})(),
        )
        async with session_factory.begin() as db:
            await publisher(db, type("Job", (), {})())
        async with session_factory() as db:
            decision = await db.scalar(select(EligibilityDecision))
            result = await db.scalar(select(ConditionResult))
            assert decision.calculated_verdict == "NEEDS_CONFIRMATION"
            assert decision.published_verdict == "NEEDS_CONFIRMATION"
            assert result.status == "UNKNOWN"
            assert AIStage.FINAL_AI_VALIDATION not in {
                invocation.stage for invocation in fake.invocations
            }

    asyncio.run(scenario())


def test_semantic_stage_is_profile_scoped_and_cannot_override_explicit_rules(
    session_factory, tmp_path: Path
) -> None:
    storage = tmp_path / "sources"

    async def scenario() -> None:
        seeded = await _seed(session_factory, storage)
        ir = _canonical_ir(seeded["source"], seeded["source_hash"], seeded["text"])
        ir["conditions"].append(
            {
                "condition_id": "semantic-fit",
                "group_id": "root",
                "kind": "MANDATORY",
                "subject": "OTHER",
                "operator": "SEMANTIC_MATCH",
                "expected_value": {"type": "STRING", "value": "소기업 역량"},
                "unit": None,
                "reference_date": None,
                "evidence": ir["conditions"][0]["evidence"],
            }
        )
        fake = FakeAIExecutor(
            {
                AIStage.CONDITION_EXTRACTION: ir,
                AIStage.SEMANTIC_JUDGMENT: {
                    "status": "PASS",
                    "explanation": "프로필과 원문이 명확히 일치합니다.",
                },
                AIStage.FINAL_AI_VALIDATION: {
                    "result": "ACCEPT",
                    "corrections": [],
                    "reason": "근거 확인",
                },
                AIStage.USER_EXPLANATION: {"explanation": "두 필수 조건을 충족합니다."},
            }
        )
        analyzer = ProductionAnnouncementAnalyzer(
            sessions=session_factory,
            executor=fake,
            source_storage_root=storage,
        )
        publisher = await analyzer.prepare(
            payload=type(
                "Payload",
                (),
                {
                    "announcement_id": seeded["announcement"],
                    "announcement_version_id": seeded["version"],
                },
            )(),
            context=type("Context", (), {})(),
        )
        async with session_factory.begin() as db:
            await publisher(db, type("Job", (), {})())
        async with session_factory() as db:
            decision = await db.scalar(
                select(EligibilityDecision).where(EligibilityDecision.is_current.is_(True))
            )
            profile_version = await db.scalar(select(CompanyProfileVersion))
            scoped_stages = list(
                (
                    await db.scalars(
                        select(AIStageRun).where(
                            AIStageRun.company_profile_version_id == profile_version.id
                        )
                    )
                ).all()
            )
            statuses = set((await db.scalars(select(ConditionResult.status))).all())
            assert decision.calculated_verdict == "ELIGIBLE"
            assert decision.published_verdict == "ELIGIBLE"
            assert decision.explanation == "두 필수 조건을 충족합니다."
            assert {stage.stage for stage in scoped_stages} == {
                "SEMANTIC_JUDGMENT",
                "FINAL_AI_VALIDATION",
                "USER_EXPLANATION",
            }
            assert statuses == {"PASS"}
            final_invocation = next(
                invocation
                for invocation in fake.invocations
                if invocation.stage == AIStage.FINAL_AI_VALIDATION
            )
            final_input = final_invocation.stdin.decode()
            assert '"profile":{"companyName":"합성 기업","companyScale":"SMALL"}' in final_input
            assert '"explanation":"프로필과 원문이 명확히 일치합니다."' in final_input

    asyncio.run(scenario())


def test_sol_stage_is_skipped_when_ai_result_cannot_change_rules_owned_verdict(
    session_factory, tmp_path: Path
) -> None:
    storage = tmp_path / "sources"

    async def scenario() -> None:
        seeded = await _seed(session_factory, storage)
        ir = _canonical_ir(seeded["source"], seeded["source_hash"], seeded["text"])
        ir["conditions"][0]["expected_value"]["value"] = "LARGE"
        ir["conditions"].append(
            {
                "condition_id": "semantic-fit",
                "group_id": "root",
                "kind": "MANDATORY",
                "subject": "OTHER",
                "operator": "SEMANTIC_MATCH",
                "expected_value": {"type": "STRING", "value": "소기업 역량"},
                "unit": None,
                "reference_date": None,
                "evidence": ir["conditions"][0]["evidence"],
            }
        )
        fake = FakeAIExecutor(
            {
                AIStage.CONDITION_EXTRACTION: ir,
                AIStage.SEMANTIC_JUDGMENT: {
                    "status": "PASS",
                    "explanation": "의미 조건은 충족합니다.",
                },
                AIStage.USER_EXPLANATION: {"explanation": "명시 조건이 불일치합니다."},
            }
        )
        analyzer = ProductionAnnouncementAnalyzer(
            sessions=session_factory,
            executor=fake,
            source_storage_root=storage,
        )
        publisher = await analyzer.prepare(
            payload=type(
                "Payload",
                (),
                {
                    "announcement_id": seeded["announcement"],
                    "announcement_version_id": seeded["version"],
                },
            )(),
            context=type("Context", (), {})(),
        )
        async with session_factory.begin() as db:
            await publisher(db, type("Job", (), {})())
        async with session_factory() as db:
            decision = await db.scalar(select(EligibilityDecision))
            assert decision.calculated_verdict == "INELIGIBLE"
            assert decision.published_verdict == "INELIGIBLE"
            assert AIStage.FINAL_AI_VALIDATION not in {
                invocation.stage for invocation in fake.invocations
            }

    asyncio.run(scenario())


def test_explanation_failure_preserves_calculation_but_publishes_safe_message(
    session_factory, tmp_path: Path
) -> None:
    storage = tmp_path / "sources"

    async def scenario() -> None:
        seeded = await _seed(session_factory, storage)
        fake = FakeAIExecutor(
            {
                AIStage.CONDITION_EXTRACTION: _canonical_ir(
                    seeded["source"], seeded["source_hash"], seeded["text"]
                )
            }
        )
        analyzer = ProductionAnnouncementAnalyzer(
            sessions=session_factory,
            executor=fake,
            source_storage_root=storage,
        )
        publisher = await analyzer.prepare(
            payload=type(
                "Payload",
                (),
                {
                    "announcement_id": seeded["announcement"],
                    "announcement_version_id": seeded["version"],
                },
            )(),
            context=type("Context", (), {})(),
        )
        async with session_factory.begin() as db:
            await publisher(db, type("Job", (), {})())
        async with session_factory() as db:
            decision = await db.scalar(select(EligibilityDecision))
            failed = await db.scalar(
                select(AIStageRun).where(AIStageRun.stage == "USER_EXPLANATION")
            )
            assert decision.calculated_verdict == "ELIGIBLE"
            assert decision.published_verdict == "NEEDS_CONFIRMATION"
            assert decision.explanation == "결과 설명 생성에 실패해 원문 확인이 필요합니다."
            assert failed.error_code == "FIXTURE_OUTPUT_MISSING"
            assert failed.company_profile_version_id == decision.company_profile_version_id

    asyncio.run(scenario())
