from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.decision_lock import serialize_decision_state
from app.decision_service import publish_deterministic_decision
from app.domain.eligibility import Evaluation, evaluate_decision
from app.enums import ConditionStatus, Verdict
from app.models import (
    AIStageRun,
    AnalysisRun,
    Announcement,
    AnnouncementAnswer,
    AnnouncementRoleSelection,
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
from app.pipeline.extraction import ExtractionFailure, decide_ocr, extract_native, render_pdf_pages
from app.pipeline.input_budget import InputDocument, build_bounded_input
from app.pipeline.ir import CanonicalIR, EvidenceSource, validate_evidence
from app.pipeline.jobs import Publisher
from app.pipeline.semantic import semantic_answer_fingerprint, semantic_input_fingerprint

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
    mime_type: str | None = None
    extracted_pages: dict[int, str] | None = None


@dataclass(frozen=True)
class SourceAnalysis:
    source: SourceInput
    text: str
    pages: dict[int, str]
    extraction_status: str
    failure_code: str | None
    used_ocr: bool = False
    ocr_images: tuple[Path, ...] = ()
    input_truncated: bool = False


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
    condition_id: str | None = None
    source_file_id: str | None = None
    answer_fingerprint: str | None = None


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


async def _latest_successful_analysis_id(
    db: AsyncSession, announcement_version_id: str
) -> str | None:
    return await db.scalar(
        select(AnalysisRun.id)
        .where(
            AnalysisRun.announcement_version_id == announcement_version_id,
            AnalysisRun.status == "SUCCEEDED",
        )
        .order_by(desc(AnalysisRun.completed_at), desc(AnalysisRun.id))
        .limit(1)
    )


async def _load_user_semantic_state(
    db: AsyncSession,
    *,
    user_id: str,
    announcement_id: str,
    announcement_version_id: str,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    answer_rows = (
        await db.execute(
            select(AnnouncementAnswer, ExtractedCondition.condition_key)
            .join(
                ExtractedCondition,
                ExtractedCondition.id == AnnouncementAnswer.condition_id,
            )
            .where(
                AnnouncementAnswer.user_id == user_id,
                AnnouncementAnswer.announcement_version_id == announcement_version_id,
                ExtractedCondition.operator == "SEMANTIC_MATCH",
            )
            .order_by(AnnouncementAnswer.created_at, AnnouncementAnswer.id)
        )
    ).all()
    answers: dict[str, dict[str, Any]] = {}
    for answer, condition_key in answer_rows:
        answers[condition_key] = {
            "value": answer.value,
            "source": answer.source,
            "memo": answer.memo,
        }
    latest_role = await db.scalar(
        select(AnnouncementRoleSelection)
        .where(
            AnnouncementRoleSelection.user_id == user_id,
            AnnouncementRoleSelection.announcement_id == announcement_id,
            AnnouncementRoleSelection.announcement_version_id == announcement_version_id,
        )
        .order_by(
            desc(AnnouncementRoleSelection.created_at),
            desc(AnnouncementRoleSelection.id),
        )
        .limit(1)
    )
    return answers, latest_role.role_key if latest_role else None


def _answer_fingerprints(answers: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {
        condition_key: fingerprint
        for condition_key, answer in answers.items()
        if (fingerprint := semantic_answer_fingerprint(answer)) is not None
    }


async def _superseded_publication(_db: AsyncSession, _job: Any) -> None:
    return None


def _parse_ocr_pages(
    output: dict[str, Any] | None, *, expected_count: int | None = None
) -> dict[int, str]:
    raw_pages = (output or {}).get("pages")
    pages: dict[int, str] = {}
    if isinstance(raw_pages, list):
        for item in raw_pages:
            if not isinstance(item, dict):
                break
            page = item.get("page")
            page_text = item.get("text")
            if (
                not isinstance(page, int)
                or isinstance(page, bool)
                or page < 1
                or not isinstance(page_text, str)
                or page in pages
            ):
                break
            pages[page] = page_text
    expected_pages = (
        set(range(1, expected_count + 1))
        if expected_count is not None
        else set(range(1, max(pages, default=0) + 1))
    )
    if not pages or set(pages) != expected_pages:
        raise ValueError("OCR output must contain every supplied page exactly once")
    return pages


def _page_tagged_text(analysis: SourceAnalysis) -> str:
    if not analysis.pages:
        return analysis.text
    return "\n".join(
        f'<page number="{page}">\n{analysis.pages[page]}\n</page>'
        for page in sorted(analysis.pages)
    )


def _is_page_capable_source(source: SourceInput) -> bool:
    suffix = Path(source.name).suffix.casefold()
    mime_type = (source.mime_type or "").split(";", 1)[0].strip().casefold()
    return suffix in {".pdf", ".png", ".jpg", ".jpeg"} or (
        mime_type == "application/pdf" or mime_type.startswith("image/")
    )


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
        condition_id: str | None = None,
        answer_fingerprint: str | None = None,
        source_file_id: str | None = None,
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
            condition_id=condition_id,
            answer_fingerprint=answer_fingerprint,
            source_file_id=source_file_id,
        )

    async def _analyze_source(
        self, source: SourceInput, temp_root: Path
    ) -> tuple[SourceAnalysis, StageResult | None]:
        if source.download_status != "SUCCEEDED":
            return SourceAnalysis(source, "", {}, "SKIPPED", "SOURCE_DOWNLOAD_INCOMPLETE"), None
        path = self._source_path(source)
        page_capable = _is_page_capable_source(source)
        restored_pages = dict(source.extracted_pages or {})
        if source.extracted_text and restored_pages:
            if path is None:
                return (
                    SourceAnalysis(
                        source,
                        "",
                        {},
                        "FAILED_FINAL",
                        "SOURCE_PAGE_PROVENANCE_MISSING",
                    ),
                    None,
                )
            try:
                if path.suffix.casefold() == ".pdf":
                    images, truncated = await asyncio.to_thread(
                        render_pdf_pages,
                        path,
                        temp_root / f"ocr-{source.source_file_id}",
                    )
                else:
                    image = temp_root / f"ocr-{source.source_file_id}{path.suffix.casefold()}"
                    await asyncio.to_thread(shutil.copy2, path, image)
                    images, truncated = (image,), False
            except (ExtractionFailure, OSError) as exc:
                code = exc.code if isinstance(exc, ExtractionFailure) else "SOURCE_READ_FAILED"
                return SourceAnalysis(source, "", {}, "FAILED_FINAL", code), None
            if len(images) != len(restored_pages):
                return (
                    SourceAnalysis(source, "", {}, "FAILED_FINAL", "OCR_PAGE_MAP_MISMATCH"),
                    None,
                )
            return (
                SourceAnalysis(
                    source,
                    source.extracted_text,
                    restored_pages,
                    "SUCCEEDED",
                    None,
                    used_ocr=True,
                    ocr_images=images,
                    input_truncated=truncated,
                ),
                None,
            )
        if source.extracted_text and not page_capable:
            return SourceAnalysis(source, source.extracted_text, {}, "SUCCEEDED", None), None
        if path is None:
            failure_code = (
                "SOURCE_PAGE_PROVENANCE_MISSING" if page_capable else "SOURCE_FILE_MISSING"
            )
            return SourceAnalysis(source, "", {}, "FAILED_FINAL", failure_code), None
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
        if path.suffix.casefold() == ".pdf":
            images, truncated = await asyncio.to_thread(
                render_pdf_pages,
                path,
                temp_root / f"ocr-{source.source_file_id}",
            )
        else:
            image = temp_root / f"ocr-{source.source_file_id}{path.suffix.casefold()}"
            await asyncio.to_thread(shutil.copy2, path, image)
            images, truncated = (image,), False
        if not images:
            return SourceAnalysis(source, "", {}, "FAILED_FINAL", "PDF_RENDER_EMPTY"), None
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
            image_paths=images,
            source_file_id=source.source_file_id,
        )
        try:
            pages = _parse_ocr_pages(stage.output, expected_count=len(images))
        except ValueError as exc:
            raise AIExecutionError(
                "OCR_OUTPUT_INVALID",
                "OCR output must contain every supplied page exactly once",
                retryable=False,
            ) from exc
        text = "\n".join(pages[page] for page in sorted(pages) if pages[page].strip())
        if not text.strip():
            raise AIExecutionError(
                "OCR_OUTPUT_INVALID", "OCR output did not contain text", retryable=False
            )
        return (
            SourceAnalysis(
                source,
                text,
                pages,
                "SUCCEEDED",
                None,
                used_ocr=True,
                ocr_images=images,
                input_truncated=truncated,
            ),
            stage,
        )

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
            ocr_stage_rows = list(
                (
                    await db.scalars(
                        select(AIStageRun)
                        .join(AnalysisRun, AnalysisRun.id == AIStageRun.analysis_run_id)
                        .where(
                            AnalysisRun.announcement_version_id == version.id,
                            AIStageRun.stage == AIStage.OCR.value,
                            AIStageRun.error_code.is_(None),
                        )
                        .order_by(desc(AnalysisRun.completed_at))
                    )
                ).all()
            )
            restored_ocr_pages: dict[str, dict[int, str]] = {}
            for stage_row in ocr_stage_rows:
                output = stage_row.structured_output
                if not isinstance(output, dict):
                    continue
                source_file_id = output.get("source_file_id")
                if not isinstance(source_file_id, str) or source_file_id in restored_ocr_pages:
                    continue
                try:
                    restored_ocr_pages[source_file_id] = _parse_ocr_pages(output)
                except ValueError:
                    continue
            sources = tuple(
                SourceInput(
                    source_file_id=row.id,
                    name=row.name,
                    storage_path=row.storage_path,
                    sha256=row.sha256,
                    source_priority=row.source_priority,
                    download_status=row.download_status,
                    extracted_text=row.extracted_text,
                    mime_type=row.mime_type,
                    extracted_pages=restored_ocr_pages.get(row.id),
                )
                for row in source_rows
            )
            profile_query = (
                select(CompanyProfileVersion)
                .join(
                    CompanyProfile,
                    (CompanyProfile.current_version_id == CompanyProfileVersion.id)
                    & (CompanyProfile.id == CompanyProfileVersion.profile_id)
                    & (CompanyProfile.user_id == CompanyProfileVersion.user_id),
                )
                .where(CompanyProfile.current_version_id.is_not(None))
                .order_by(CompanyProfileVersion.user_id, CompanyProfileVersion.id)
            )
            target_profile_version_id = getattr(payload, "company_profile_version_id", None)
            if target_profile_version_id is not None:
                profile_query = profile_query.where(
                    CompanyProfileVersion.id == target_profile_version_id
                )
            profile_rows = list((await db.scalars(profile_query)).all())
            if target_profile_version_id is not None and not profile_rows:
                raise AIExecutionError(
                    "ANALYSIS_PROFILE_VERSION_INVALID",
                    "Target company profile version is missing or no longer current",
                    retryable=False,
                )
            profiles = tuple(
                ProfileInput(row.user_id, row.id, dict(row.snapshot)) for row in profile_rows
            )
            latest_semantic_answers: dict[tuple[str, str], dict[str, Any]] = {}
            selected_roles: dict[str, str | None] = {}
            profile_answers: dict[str, dict[str, dict[str, Any]]] = {}
            for profile in profiles:
                answers, selected_role = await _load_user_semantic_state(
                    db,
                    user_id=profile.user_id,
                    announcement_id=announcement.id,
                    announcement_version_id=version.id,
                )
                profile_answers[profile.user_id] = answers
                selected_roles[profile.user_id] = selected_role
                latest_semantic_answers.update(
                    {
                        (profile.user_id, condition_key): answer
                        for condition_key, answer in answers.items()
                    }
                )

            semantic_input_hash = getattr(payload, "semantic_input_hash", None)
            semantic_base_analysis_run_id = getattr(payload, "semantic_base_analysis_run_id", None)
            if semantic_input_hash is not None or semantic_base_analysis_run_id is not None:
                if (
                    semantic_input_hash is None
                    or semantic_base_analysis_run_id is None
                    or target_profile_version_id is None
                    or len(profiles) != 1
                    or await _latest_successful_analysis_id(db, version.id)
                    != semantic_base_analysis_run_id
                ):
                    return _superseded_publication
                profile = profiles[0]
                actual_input_hash = semantic_input_fingerprint(
                    analysis_run_id=semantic_base_analysis_run_id,
                    answer_fingerprints=_answer_fingerprints(profile_answers[profile.user_id]),
                    selected_role_key=selected_roles[profile.user_id],
                )
                if actual_input_hash != semantic_input_hash:
                    return _superseded_publication

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
                        _page_tagged_text(item),
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
                    "source_page_map": [
                        {
                            "source_file_id": item.source.source_file_id,
                            "source_version": item.source.sha256,
                            "pages": sorted(item.pages),
                        }
                        for item in source_analyses
                        if item.pages
                        and item.source.source_file_id in bounded.included_source_file_ids
                    ],
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
                    page_capable=_is_page_capable_source(item.source),
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
                item.failure_code is not None or item.input_truncated for item in source_analyses
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
                    image_paths=tuple(
                        image
                        for item in source_analyses
                        if item.source.source_file_id in ocr_source_ids
                        for image in item.ocr_images
                    ),
                )
                stages.append(ocr_validation)
                if (ocr_validation.output or {}).get("result") != "ACCEPT":
                    safety_unknown_condition_ids.update(ocr_condition_ids)
            for profile in profiles:
                selected_role_key = selected_roles[profile.user_id]
                profile_safety_unknown_ids = set(safety_unknown_condition_ids)
                semantic_evaluations: dict[str, Evaluation] = {}
                profile_stages: list[StageResult] = []
                for condition in canonical_ir.conditions:
                    if condition.operator.value != "SEMANTIC_MATCH":
                        continue
                    answer = latest_semantic_answers.get((profile.user_id, condition.condition_id))
                    answer_fingerprint = semantic_answer_fingerprint(answer)
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
                            "selected_role_key": selected_role_key,
                            "latest_answer": answer,
                        },
                        company_profile_version_id=profile.version_id,
                        condition_id=condition.condition_id,
                        answer_fingerprint=answer_fingerprint,
                    )
                    profile_stages.append(semantic_stage)
                    returned_condition_id = (semantic_stage.output or {}).get("condition_id")
                    if returned_condition_id not in {None, condition.condition_id}:
                        raise AIExecutionError(
                            "SEMANTIC_OUTPUT_INVALID",
                            "Semantic judgment returned a mismatched condition ID",
                            retryable=False,
                        )
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
                    selected_role_key,
                    semantic_evaluations=semantic_evaluations,
                    safety_unknown_condition_ids=profile_safety_unknown_ids,
                )
                force_confirmation = False
                ai_dependent_ids = set(semantic_evaluations) | ocr_condition_ids
                counterfactual = evaluate_decision(
                    ir_json,
                    profile.snapshot,
                    selected_role_key,
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
                            "profile": profile.snapshot,
                            "calculated_verdict": calculated.verdict.value,
                            "semantic_results": {
                                key: {
                                    "status": value.status.value,
                                    "explanation": value.explanation,
                                }
                                for key, value in semantic_evaluations.items()
                            },
                            "latest_answers": {
                                condition_id: latest_semantic_answers.get(
                                    (profile.user_id, condition_id)
                                )
                                for condition_id in semantic_evaluations
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
                        selected_role_key,
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
            if semantic_input_hash is not None:
                if (
                    semantic_base_analysis_run_id is None
                    or target_profile_version_id is None
                    or len(profiles) != 1
                ):
                    return
                profile = profiles[0]
                await serialize_decision_state(
                    db,
                    user_id=profile.user_id,
                    announcement_id=announcement.id,
                )
                current_announcement_version_id = await db.scalar(
                    select(Announcement.current_version_id).where(
                        Announcement.id == announcement.id
                    )
                )
                current_profile_version_id = await db.scalar(
                    select(CompanyProfileVersion.id)
                    .join(
                        CompanyProfile,
                        (CompanyProfile.current_version_id == CompanyProfileVersion.id)
                        & (CompanyProfile.id == CompanyProfileVersion.profile_id)
                        & (CompanyProfile.user_id == CompanyProfileVersion.user_id),
                    )
                    .where(CompanyProfileVersion.id == target_profile_version_id)
                )
                current_answers, current_role = await _load_user_semantic_state(
                    db,
                    user_id=profile.user_id,
                    announcement_id=announcement.id,
                    announcement_version_id=version.id,
                )
                current_input_hash = semantic_input_fingerprint(
                    analysis_run_id=semantic_base_analysis_run_id,
                    answer_fingerprints=_answer_fingerprints(current_answers),
                    selected_role_key=current_role,
                )
                if (
                    current_announcement_version_id != version.id
                    or current_profile_version_id != target_profile_version_id
                    or await _latest_successful_analysis_id(db, version.id)
                    != semantic_base_analysis_run_id
                    or current_input_hash != semantic_input_hash
                ):
                    return
            now = datetime.now(UTC)
            analysis = await db.scalar(
                select(AnalysisRun).where(
                    AnalysisRun.announcement_version_id == version.id,
                    AnalysisRun.status == "PENDING",
                    AnalysisRun.analysis_version == "attachment-selection-v1",
                )
            )
            if analysis is None:
                analysis = AnalysisRun(
                    announcement_version_id=version.id,
                    status="SUCCEEDED",
                    analysis_version=canonical_ir.analysis_version,
                    canonical_ir=canonical_ir.model_dump(mode="json"),
                    started_at=now,
                    completed_at=now,
                )
                db.add(analysis)
            else:
                analysis.status = "SUCCEEDED"
                analysis.analysis_version = canonical_ir.analysis_version
                analysis.canonical_ir = canonical_ir.model_dump(mode="json")
                analysis.completed_at = now
            analysis.error_code = "ATTACHMENT_INPUT_TRUNCATED" if bounded.truncated else None
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
                        structured_output=(
                            {
                                **stage.output,
                                **(
                                    {
                                        "condition_id": stage.condition_id,
                                        "answer_fingerprint": stage.answer_fingerprint,
                                    }
                                    if stage.condition_id is not None
                                    else {}
                                ),
                                **(
                                    {"source_file_id": stage.source_file_id}
                                    if stage.source_file_id is not None
                                    else {}
                                ),
                            }
                            if stage.output is not None
                            else None
                        ),
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
                await serialize_decision_state(
                    db,
                    user_id=profile.user_id,
                    announcement_id=announcement.id,
                )
                current_announcement_version_id = await db.scalar(
                    select(Announcement.current_version_id).where(
                        Announcement.id == announcement.id
                    )
                )
                current_profile_version_id = await db.scalar(
                    select(CompanyProfile.current_version_id).where(
                        CompanyProfile.user_id == profile.user_id,
                        CompanyProfile.current_version_id == profile.version_id,
                    )
                )
                current_analysis_id = await _latest_successful_analysis_id(db, version.id)
                current_answers, current_role = await _load_user_semantic_state(
                    db,
                    user_id=profile.user_id,
                    announcement_id=announcement.id,
                    announcement_version_id=version.id,
                )
                if (
                    current_announcement_version_id != version.id
                    or current_profile_version_id != profile.version_id
                    or current_analysis_id != analysis.id
                    or current_answers != profile_answers[profile.user_id]
                    or current_role != selected_roles[profile.user_id]
                ):
                    continue
                decision = await publish_deterministic_decision(
                    db,
                    user_id=profile.user_id,
                    announcement_id=announcement.id,
                    announcement_version_id=version.id,
                    company_profile_version_id=profile.version_id,
                    analysis_run_id=analysis.id,
                    selected_role_key=selected_roles[profile.user_id],
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
