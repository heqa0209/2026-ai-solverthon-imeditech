from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.decision_service import publish_deterministic_decision
from app.domain.eligibility import Evaluation, evaluate_decision
from app.enums import ConditionStatus, Verdict
from app.models import (
    AIStageRun,
    AnalysisRun,
    Announcement,
    AnnouncementVersion,
    CompanyProfile,
    CompanyProfileVersion,
    ExtractedCondition,
    SourceFile,
)
from app.pipeline.ai import (
    AI_STAGE_POLICIES,
    AIExecutionError,
    AIExecutor,
    AIStage,
    build_codex_invocation,
)
from app.pipeline.extraction import ExtractionFailure, decide_ocr, extract_native
from app.pipeline.input_budget import InputDocument, build_bounded_input
from app.pipeline.ir import CanonicalIR, EvidenceSource, validate_evidence
from app.pipeline.jobs import Publisher

CONTRACT_ROOT = Path(__file__).with_name("contracts")
PROMPT_VERSION = "condition-extraction-v1"
SCHEMA_VERSION = "canonical-ir-v1"


@dataclass(frozen=True)
class SourceInput:
    source_file_id: str
    name: str
    storage_path: str | None
    sha256: str | None
    source_priority: int
    download_status: str
    extracted_text: str | None


@dataclass(frozen=True)
class SourceAnalysis:
    source: SourceInput
    text: str
    pages: dict[int, str]
    extraction_status: str
    failure_code: str | None
    used_ocr: bool = False


@dataclass(frozen=True)
class StageResult:
    stage: AIStage
    model: str
    effort: str
    input_hash: str
    output: dict[str, Any] | None
    duration_ms: int
    prompt_version: str
    schema_version: str
    company_profile_version_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class ProfileInput:
    user_id: str
    version_id: str
    snapshot: dict[str, Any]


@dataclass(frozen=True)
class ProfileAnalysis:
    profile: ProfileInput
    semantic_evaluations: dict[str, Evaluation]
    safety_unknown_condition_ids: frozenset[str]
    stages: tuple[StageResult, ...]
    explanation: str | None
    force_confirmation: bool


class ProductionAnnouncementAnalyzer:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        executor: AIExecutor,
        source_storage_root: Path,
        contract_root: Path = CONTRACT_ROOT,
    ):
        self._sessions = sessions
        self._executor = executor
        self._storage_root = source_storage_root.resolve()
        self._contract_root = contract_root.resolve()

    def _source_path(self, source: SourceInput) -> Path | None:
        if source.storage_path is None:
            return None
        candidate = Path(source.storage_path)
        path = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self._storage_root / candidate).resolve()
        )
        if not path.is_relative_to(self._storage_root) or not path.is_file():
            return None
        return path

    async def _run_stage(
        self,
        *,
        stage: AIStage,
        temp_root: Path,
        instruction_path: Path,
        schema_path: Path,
        structured_input: dict[str, Any],
        image_paths: tuple[Path, ...] = (),
        company_profile_version_id: str | None = None,
        capture_failure: bool = False,
    ) -> StageResult:
        local_schema = temp_root / schema_path.name
        await asyncio.to_thread(shutil.copy2, schema_path, local_schema)
        output = temp_root / f"{stage.value.casefold()}-output.json"
        instruction = await asyncio.to_thread(instruction_path.read_text, encoding="utf-8")
        invocation = build_codex_invocation(
            stage=stage,
            temp_dir=temp_root,
            schema_path=local_schema,
            output_path=output,
            instruction=instruction,
            structured_input=structured_input,
            image_paths=image_paths,
        )
        started = time.monotonic()
        try:
            result = await self._executor.execute(invocation)
            error_code = None
        except AIExecutionError as exc:
            if not capture_failure:
                raise
            result = None
            error_code = exc.code
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        policy = AI_STAGE_POLICIES[stage]
        return StageResult(
            stage=stage,
            model=policy.model,
            effort=policy.effort,
            input_hash=invocation.input_hash,
            output=result,
            duration_ms=duration_ms,
            prompt_version=instruction_path.stem,
            schema_version=schema_path.stem,
            company_profile_version_id=company_profile_version_id,
            error_code=error_code,
        )

    async def _analyze_source(
        self, source: SourceInput, temp_root: Path
    ) -> tuple[SourceAnalysis, StageResult | None]:
        if source.download_status != "SUCCEEDED":
            return SourceAnalysis(source, "", {}, "SKIPPED", "SOURCE_DOWNLOAD_INCOMPLETE"), None
        if source.extracted_text:
            return SourceAnalysis(source, source.extracted_text, {}, "SUCCEEDED", None), None
        path = self._source_path(source)
        if path is None:
            return SourceAnalysis(source, "", {}, "FAILED_FINAL", "SOURCE_FILE_MISSING"), None
        try:
            if path.suffix.casefold() == ".txt":
                text = await asyncio.to_thread(path.read_text, encoding="utf-8")
                return SourceAnalysis(source, text, {}, "SUCCEEDED", None), None
            extraction = await asyncio.to_thread(extract_native, path)
        except (ExtractionFailure, OSError) as exc:
            code = exc.code if isinstance(exc, ExtractionFailure) else "SOURCE_READ_FAILED"
            return SourceAnalysis(source, "", {}, "FAILED_FINAL", code), None
        if extraction.text:
            pages: dict[int, str] = {}
            for segment in extraction.segments:
                if segment.page is not None:
                    pages[segment.page] = "\n".join(
                        part for part in (pages.get(segment.page), segment.text) if part
                    )
            return SourceAnalysis(source, extraction.text, pages, "SUCCEEDED", None), None

        ocr = decide_ocr(path, extraction)
        if not ocr.required or path.suffix.casefold() not in {".pdf", ".png", ".jpg", ".jpeg"}:
            return SourceAnalysis(source, "", {}, "SKIPPED", "OCR_FORMAT_UNSUPPORTED"), None
        image = temp_root / f"ocr-{source.source_file_id}{path.suffix.casefold()}"
        await asyncio.to_thread(shutil.copy2, path, image)
        stage = await self._run_stage(
            stage=AIStage.OCR,
            temp_root=temp_root,
            instruction_path=self._contract_root / "ocr-v1.prompt.txt",
            schema_path=self._contract_root / "ocr-v1.schema.json",
            structured_input={
                "source_file_id": source.source_file_id,
                "source_version": source.sha256,
                "filename": source.name,
            },
            image_paths=(image,),
        )
        text = stage.output.get("text")
        if not isinstance(text, str) or not text.strip():
            raise AIExecutionError(
                "OCR_OUTPUT_INVALID", "OCR output did not contain text", retryable=False
            )
        return SourceAnalysis(source, text, {1: text}, "SUCCEEDED", None, used_ocr=True), stage

    async def prepare(self, payload: Any, context: Any) -> Publisher:
        if not payload.announcement_id or not payload.announcement_version_id:
            raise AIExecutionError(
                "ANALYSIS_JOB_PAYLOAD_INVALID",
                "Announcement and version are required",
                retryable=False,
            )
        async with self._sessions() as db:
            announcement = await db.get(Announcement, payload.announcement_id)
            version = await db.get(AnnouncementVersion, payload.announcement_version_id)
            if (
                announcement is None
                or version is None
                or version.announcement_id != announcement.id
                or announcement.current_version_id != version.id
            ):
                raise AIExecutionError(
                    "ANALYSIS_INPUTS_INVALID",
                    "Announcement analysis inputs do not match",
                    retryable=False,
                )
            source_rows = list(
                (
                    await db.scalars(
                        select(SourceFile)
                        .where(SourceFile.announcement_version_id == version.id)
                        .order_by(SourceFile.source_order, SourceFile.id)
                    )
                ).all()
            )
            sources = tuple(
                SourceInput(
                    source_file_id=row.id,
                    name=row.name,
                    storage_path=row.storage_path,
                    sha256=row.sha256,
                    source_priority=row.source_priority,
                    download_status=row.download_status,
                    extracted_text=row.extracted_text,
                )
                for row in source_rows
            )
            profile_rows = list(
                (
                    await db.scalars(
                        select(CompanyProfileVersion)
                        .join(
                            CompanyProfile,
                            CompanyProfile.current_version_id == CompanyProfileVersion.id,
                        )
                        .where(CompanyProfile.current_version_id.is_not(None))
                    )
                ).all()
            )
            profiles = tuple(
                ProfileInput(row.user_id, row.id, dict(row.snapshot)) for row in profile_rows
            )

        job_id = getattr(context, "job_id", "manual")
        with tempfile.TemporaryDirectory(prefix=f"solverthon-analysis-{job_id}-") as directory:
            temp_root = await asyncio.to_thread(Path(directory).resolve)
            source_analyses: list[SourceAnalysis] = []
            stages: list[StageResult] = []
            for source in sources:
                analyzed, ocr_stage = await self._analyze_source(source, temp_root)
                source_analyses.append(analyzed)
                if ocr_stage is not None:
                    stages.append(ocr_stage)
            bounded = build_bounded_input(
                [
                    InputDocument(
                        item.source.source_file_id,
                        item.source.source_priority,
                        item.text,
                    )
                    for item in source_analyses
                    if item.text
                ]
            )
            if not bounded.text:
                raise AIExecutionError(
                    "ANALYSIS_INPUT_EMPTY",
                    "No source text is available for analysis",
                    retryable=False,
                )
            condition_stage = await self._run_stage(
                stage=AIStage.CONDITION_EXTRACTION,
                temp_root=temp_root,
                instruction_path=self._contract_root / "condition-extraction-v1.prompt.txt",
                schema_path=self._contract_root / "canonical-ir-v1.schema.json",
                structured_input={
                    "announcement_id": announcement.id,
                    "announcement_version_id": version.id,
                    "source_text": bounded.text,
                    "omitted_source_file_ids": list(bounded.omitted_source_file_ids),
                },
            )
            stages.append(condition_stage)
            validated_sources = [
                EvidenceSource(
                    source_file_id=item.source.source_file_id,
                    source_version=item.source.sha256 or "",
                    text=item.text,
                    pages=item.pages,
                )
                for item in source_analyses
                if item.text
            ]
            try:
                canonical_ir = CanonicalIR.model_validate(condition_stage.output)
                validate_evidence(canonical_ir, validated_sources)
            except ValueError as exc:
                raise AIExecutionError(
                    "CANONICAL_IR_INVALID",
                    "Condition extraction did not pass schema and evidence validation",
                    retryable=False,
                ) from exc
            incomplete = bounded.truncated or any(
                item.failure_code is not None for item in source_analyses
            )
            profile_analyses: list[ProfileAnalysis] = []
            ir_json = canonical_ir.model_dump(mode="json")
            ocr_source_ids = {
                item.source.source_file_id for item in source_analyses if item.used_ocr
            }
            ocr_condition_ids = {
                condition.condition_id
                for condition in canonical_ir.conditions
                if condition.kind.value == "MANDATORY"
                and any(
                    evidence.source_file_id in ocr_source_ids for evidence in condition.evidence
                )
            }
            safety_unknown_condition_ids: set[str] = set()
            if ocr_condition_ids:
                ocr_validation = await self._run_stage(
                    stage=AIStage.OCR_EVIDENCE_VALIDATION,
                    temp_root=temp_root,
                    instruction_path=self._contract_root / "ocr-evidence-validation-v1.prompt.txt",
                    schema_path=self._contract_root / "ocr-evidence-validation-v1.schema.json",
                    structured_input={
                        "announcement_version_id": version.id,
                        "conditions": [
                            condition.model_dump(mode="json")
                            for condition in canonical_ir.conditions
                            if condition.condition_id in ocr_condition_ids
                        ],
                        "ocr_sources": [
                            {
                                "source_file_id": item.source.source_file_id,
                                "source_version": item.source.sha256,
                                "text": item.text,
                            }
                            for item in source_analyses
                            if item.source.source_file_id in ocr_source_ids
                        ],
                    },
                )
                stages.append(ocr_validation)
                if (ocr_validation.output or {}).get("result") != "ACCEPT":
                    safety_unknown_condition_ids.update(ocr_condition_ids)
            for profile in profiles:
                profile_safety_unknown_ids = set(safety_unknown_condition_ids)
                semantic_evaluations: dict[str, Evaluation] = {}
                profile_stages: list[StageResult] = []
                for condition in canonical_ir.conditions:
                    if condition.operator.value != "SEMANTIC_MATCH":
                        continue
                    semantic_stage = await self._run_stage(
                        stage=AIStage.SEMANTIC_JUDGMENT,
                        temp_root=temp_root,
                        instruction_path=self._contract_root / "semantic-judgment-v1.prompt.txt",
                        schema_path=self._contract_root / "semantic-judgment-v1.schema.json",
                        structured_input={
                            "announcement_version_id": version.id,
                            "company_profile_version_id": profile.version_id,
                            "profile": profile.snapshot,
                            "condition": condition.model_dump(mode="json"),
                        },
                        company_profile_version_id=profile.version_id,
                    )
                    profile_stages.append(semantic_stage)
                    status = (semantic_stage.output or {}).get("status")
                    if status not in {"PASS", "FAIL", "UNKNOWN"}:
                        raise AIExecutionError(
                            "SEMANTIC_OUTPUT_INVALID",
                            "Semantic judgment returned an invalid status",
                            retryable=False,
                        )
                    semantic_evaluations[condition.condition_id] = Evaluation(
                        ConditionStatus(status),
                        explanation=(semantic_stage.output or {}).get("explanation"),
                    )
                calculated = evaluate_decision(
                    ir_json,
                    profile.snapshot,
                    None,
                    semantic_evaluations=semantic_evaluations,
                    safety_unknown_condition_ids=profile_safety_unknown_ids,
                )
                force_confirmation = False
                ai_dependent_ids = set(semantic_evaluations) | ocr_condition_ids
                counterfactual = evaluate_decision(
                    ir_json,
                    profile.snapshot,
                    None,
                    semantic_evaluations=semantic_evaluations,
                    safety_unknown_condition_ids=(profile_safety_unknown_ids | ai_dependent_ids),
                )
                if ai_dependent_ids and counterfactual.verdict != calculated.verdict:
                    validation_stage = await self._run_stage(
                        stage=AIStage.FINAL_AI_VALIDATION,
                        temp_root=temp_root,
                        instruction_path=self._contract_root / "final-validation-v1.prompt.txt",
                        schema_path=self._contract_root / "final-validation-v1.schema.json",
                        structured_input={
                            "announcement_version_id": version.id,
                            "company_profile_version_id": profile.version_id,
                            "calculated_verdict": calculated.verdict.value,
                            "semantic_results": {
                                key: value.status.value
                                for key, value in semantic_evaluations.items()
                            },
                            "ocr_condition_ids": sorted(ocr_condition_ids),
                            "conditions": [
                                condition.model_dump(mode="json")
                                for condition in canonical_ir.conditions
                                if condition.condition_id in ai_dependent_ids
                            ],
                        },
                        company_profile_version_id=profile.version_id,
                    )
                    profile_stages.append(validation_stage)
                    validation_output = validation_stage.output or {}
                    validation_result = validation_output.get("result")
                    corrections = validation_output.get("corrections", [])
                    if validation_result == "CORRECT" and isinstance(corrections, list):
                        correction_valid = bool(corrections)
                        for correction in corrections:
                            condition_id = correction.get("condition_id")
                            status = correction.get("status")
                            if condition_id in semantic_evaluations and status in {
                                "PASS",
                                "FAIL",
                                "UNKNOWN",
                            }:
                                semantic_evaluations[condition_id] = Evaluation(
                                    ConditionStatus(status),
                                    explanation="최종 AI 검증으로 의미판단을 정정했습니다.",
                                )
                            elif condition_id in ocr_condition_ids:
                                profile_safety_unknown_ids.add(condition_id)
                            else:
                                correction_valid = False
                        if not correction_valid:
                            profile_safety_unknown_ids.update(ai_dependent_ids)
                        validate_evidence(canonical_ir, validated_sources)
                    elif validation_result != "ACCEPT":
                        profile_safety_unknown_ids.update(ai_dependent_ids)
                    calculated = evaluate_decision(
                        ir_json,
                        profile.snapshot,
                        None,
                        semantic_evaluations=semantic_evaluations,
                        safety_unknown_condition_ids=profile_safety_unknown_ids,
                    )
                explanation_stage = await self._run_stage(
                    stage=AIStage.USER_EXPLANATION,
                    temp_root=temp_root,
                    instruction_path=self._contract_root / "user-explanation-v1.prompt.txt",
                    schema_path=self._contract_root / "user-explanation-v1.schema.json",
                    structured_input={
                        "announcement_version_id": version.id,
                        "company_profile_version_id": profile.version_id,
                        "profile": profile.snapshot,
                        "calculated_verdict": calculated.verdict.value,
                        "conditions": [
                            {
                                "condition_id": condition.condition_id,
                                "status": calculated.conditions.get(
                                    condition.condition_id,
                                    Evaluation(ConditionStatus.UNKNOWN),
                                ).status.value,
                                "evidence": [
                                    item.model_dump(mode="json") for item in condition.evidence
                                ],
                            }
                            for condition in canonical_ir.conditions
                        ],
                    },
                    company_profile_version_id=profile.version_id,
                    capture_failure=True,
                )
                profile_stages.append(explanation_stage)
                explanation = (
                    (explanation_stage.output or {}).get("explanation")
                    if explanation_stage.error_code is None
                    else None
                )
                if not isinstance(explanation, str) or not explanation.strip():
                    explanation = None
                    force_confirmation = True
                profile_analyses.append(
                    ProfileAnalysis(
                        profile,
                        semantic_evaluations,
                        frozenset(profile_safety_unknown_ids),
                        tuple(profile_stages),
                        explanation,
                        force_confirmation,
                    )
                )

        async def publish(db: AsyncSession, _job: Any) -> None:
            now = datetime.now(UTC)
            analysis = AnalysisRun(
                announcement_version_id=version.id,
                status="SUCCEEDED",
                analysis_version=canonical_ir.analysis_version,
                canonical_ir=canonical_ir.model_dump(mode="json"),
                started_at=now,
                completed_at=now,
                error_code="ATTACHMENT_INPUT_TRUNCATED" if bounded.truncated else None,
            )
            db.add(analysis)
            await db.flush()
            for item in source_analyses:
                row = await db.get(SourceFile, item.source.source_file_id)
                if row is not None:
                    row.extracted_text = item.text or None
                    row.extraction_status = item.extraction_status
                    if item.failure_code is not None:
                        row.failure_code = item.failure_code
            all_stages = [*stages, *(stage for item in profile_analyses for stage in item.stages)]
            for stage in all_stages:
                evidence = []
                if stage.stage == AIStage.CONDITION_EXTRACTION:
                    evidence = [
                        evidence.model_dump(mode="json")
                        for condition in canonical_ir.conditions
                        for evidence in condition.evidence
                    ]
                db.add(
                    AIStageRun(
                        analysis_run_id=analysis.id,
                        company_profile_version_id=stage.company_profile_version_id,
                        stage=stage.stage.value,
                        model=stage.model,
                        effort=stage.effort,
                        prompt_version=stage.prompt_version,
                        schema_version=stage.schema_version,
                        input_hash=stage.input_hash,
                        structured_output=stage.output,
                        evidence=evidence,
                        duration_ms=stage.duration_ms,
                        attempt=1,
                        error_code=stage.error_code,
                    )
                )
            groups = {group.group_id: group for group in canonical_ir.groups}
            for condition in canonical_ir.conditions:
                group = groups[condition.group_id]
                db.add(
                    ExtractedCondition(
                        analysis_run_id=analysis.id,
                        condition_key=condition.condition_id,
                        group_key=condition.group_id,
                        track_key=group.track_ids[0] if len(group.track_ids) == 1 else None,
                        role_key=group.role_keys[0] if len(group.role_keys) == 1 else None,
                        kind=condition.kind.value,
                        subject=condition.subject.value,
                        operator=condition.operator.value,
                        expected_value=(
                            condition.expected_value.model_dump(mode="json")
                            if condition.expected_value is not None
                            else None
                        ),
                        unit=condition.unit,
                        reference_date=condition.reference_date,
                        evidence=[item.model_dump(mode="json") for item in condition.evidence],
                    )
                )
            await db.flush()
            for profile_analysis in profile_analyses:
                profile = profile_analysis.profile
                decision = await publish_deterministic_decision(
                    db,
                    user_id=profile.user_id,
                    announcement_id=announcement.id,
                    announcement_version_id=version.id,
                    company_profile_version_id=profile.version_id,
                    analysis_run_id=analysis.id,
                    selected_role_key=None,
                    explanation=profile_analysis.explanation,
                    semantic_evaluations=profile_analysis.semantic_evaluations,
                    safety_unknown_condition_ids=set(profile_analysis.safety_unknown_condition_ids),
                )
                if incomplete or profile_analysis.force_confirmation:
                    decision.published_verdict = Verdict.NEEDS_CONFIRMATION.value
                    decision.decision_origin = "SYSTEM_FAILURE"
                    decision.explanation = (
                        "일부 첨부 또는 입력을 확인하지 못해 원문 확인이 필요합니다."
                        if incomplete
                        else "결과 설명 생성에 실패해 원문 확인이 필요합니다."
                    )

        return publish
