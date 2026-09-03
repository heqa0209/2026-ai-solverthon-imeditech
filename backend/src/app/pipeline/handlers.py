from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.decision_lock import serialize_decision_state
from app.decision_service import publish_analysis_failure, publish_deterministic_decision
from app.domain.eligibility import Evaluation
from app.enums import ConditionStatus
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
    ExtractedCondition,
)
from app.pipeline.ai import AIExecutionError
from app.pipeline.jobs import Publisher
from app.pipeline.persistence import persist_demo_fixture
from app.pipeline.semantic import semantic_answer_fingerprint, semantic_input_fingerprint
from app.pipeline.worker import JobContext, JobHandler, JobOutcome


class StrictJobPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DecisionReevaluatePayload(StrictJobPayload):
    user_id: str
    announcement_id: str
    announcement_version_id: str
    company_profile_version_id: str
    selected_role_key: str | None = None
    cause: str
    answer: dict[str, Any] | None = None


class AnnouncementAnalyzePayload(StrictJobPayload):
    announcement_id: str | None = None
    announcement_version_id: str | None = None
    fixture_manifest: str | None = None
    company_profile_version_id: str | None = None
    requested_by_job_id: str | None = None
    semantic_input_hash: str | None = None
    semantic_base_analysis_run_id: str | None = None
    requested_at: str | None = None


class AnnouncementAnalyzer(Protocol):
    async def prepare(
        self, payload: AnnouncementAnalyzePayload, context: JobContext
    ) -> Publisher: ...


class CollectionPayload(StrictJobPayload):
    scope: Literal["DAILY", "FULL"]
    scheduled_for: str | None = None
    requested_at: str | None = None


class AnnouncementCollector(Protocol):
    async def prepare(self, *, scope: str, context: JobContext) -> Publisher: ...


def _normalize_payload(raw: dict[str, Any]) -> dict[str, Any]:
    aliases = {
        "userId": "user_id",
        "announcementId": "announcement_id",
        "announcementVersionId": "announcement_version_id",
        "companyProfileVersionId": "company_profile_version_id",
        "selectedRoleKey": "selected_role_key",
        "fixtureManifest": "fixture_manifest",
        "scheduledFor": "scheduled_for",
        "requestedAt": "requested_at",
        "requestedByJobId": "requested_by_job_id",
        "semanticInputHash": "semantic_input_hash",
        "semanticBaseAnalysisRunId": "semantic_base_analysis_run_id",
    }
    return {aliases.get(key, key): value for key, value in raw.items()}


async def _load_semantic_evaluations(
    db: AsyncSession,
    *,
    analysis: AnalysisRun,
    company_profile_version_id: str,
    answer_fingerprints: dict[str, str],
) -> dict[str, Evaluation] | None:
    semantic_ids = {
        str(condition.get("condition_id") or condition.get("conditionId"))
        for condition in (analysis.canonical_ir or {}).get("conditions", [])
        if isinstance(condition, dict) and condition.get("operator") == "SEMANTIC_MATCH"
    }
    if not semantic_ids:
        return {}

    stages = list(
        (
            await db.scalars(
                select(AIStageRun).where(
                    AIStageRun.analysis_run_id == analysis.id,
                    AIStageRun.company_profile_version_id == company_profile_version_id,
                    AIStageRun.stage.in_(["SEMANTIC_JUDGMENT", "FINAL_AI_VALIDATION"]),
                    AIStageRun.error_code.is_(None),
                )
            )
        ).all()
    )
    evaluations: dict[str, Evaluation] = {}
    for stage in stages:
        if stage.stage != "SEMANTIC_JUDGMENT" or not isinstance(stage.structured_output, dict):
            continue
        condition_id = stage.structured_output.get("condition_id")
        status = stage.structured_output.get("status")
        if condition_id in semantic_ids and status in {"PASS", "FAIL", "UNKNOWN"}:
            if stage.structured_output.get("answer_fingerprint") != answer_fingerprints.get(
                condition_id
            ):
                continue
            evaluations[condition_id] = Evaluation(
                ConditionStatus(status),
                explanation=stage.structured_output.get("explanation"),
            )
    if set(evaluations) != semantic_ids:
        return None

    validation = next((stage for stage in stages if stage.stage == "FINAL_AI_VALIDATION"), None)
    if validation is None:
        return evaluations
    output = validation.structured_output
    if not isinstance(output, dict):
        return None
    result = output.get("result")
    if result == "ACCEPT":
        return evaluations
    if result == "UNRESOLVED":
        return {
            condition_id: Evaluation(
                ConditionStatus.UNKNOWN,
                explanation="최종 AI 검증에서 의미판단을 확정하지 못했습니다.",
            )
            for condition_id in semantic_ids
        }
    if result != "CORRECT" or not isinstance(output.get("corrections"), list):
        return None
    for correction in output["corrections"]:
        if not isinstance(correction, dict):
            return None
        condition_id = correction.get("condition_id")
        status = correction.get("status")
        if condition_id not in semantic_ids or status not in {"PASS", "FAIL", "UNKNOWN"}:
            return None
        evaluations[condition_id] = Evaluation(
            ConditionStatus(status),
            explanation="최종 AI 검증으로 의미판단을 정정했습니다.",
        )
    return evaluations


async def _latest_semantic_answer_fingerprints(
    db: AsyncSession,
    *,
    user_id: str,
    announcement_version_id: str,
) -> dict[str, str]:
    rows = (
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
    latest: dict[str, str] = {}
    for answer, condition_key in rows:
        fingerprint = semantic_answer_fingerprint(
            {"value": answer.value, "source": answer.source, "memo": answer.memo}
        )
        if fingerprint is not None:
            latest[condition_key] = fingerprint
    return latest


def decision_reevaluate_handler(
    sessions: async_sessionmaker[AsyncSession],
) -> JobHandler:
    async def handle(raw: dict[str, Any], _context: JobContext) -> JobOutcome:
        try:
            payload = DecisionReevaluatePayload.model_validate(_normalize_payload(raw))
        except ValueError as exc:
            raise AIExecutionError(
                "DECISION_JOB_PAYLOAD_INVALID", "Decision job payload is invalid", retryable=False
            ) from exc
        async with sessions() as db:
            announcement = await db.get(Announcement, payload.announcement_id)
            version = await db.get(AnnouncementVersion, payload.announcement_version_id)
            profile = await db.get(CompanyProfileVersion, payload.company_profile_version_id)
            if (
                announcement is None
                or version is None
                or version.announcement_id != announcement.id
                or profile is None
                or profile.user_id != payload.user_id
            ):
                raise AIExecutionError(
                    "DECISION_INPUTS_INVALID", "Decision inputs do not match", retryable=False
                )
            analysis = await db.scalar(
                select(AnalysisRun)
                .where(
                    AnalysisRun.announcement_version_id == version.id,
                    AnalysisRun.status == "SUCCEEDED",
                )
                .order_by(desc(AnalysisRun.completed_at), desc(AnalysisRun.id))
                .limit(1)
            )
            if analysis is None:
                raise AIExecutionError(
                    "ANALYSIS_NOT_READY", "No successful analysis is available", retryable=True
                )
            selected_role = payload.selected_role_key
            if "selected_role_key" not in payload.model_fields_set:
                latest_role = await db.scalar(
                    select(AnnouncementRoleSelection)
                    .where(
                        AnnouncementRoleSelection.user_id == payload.user_id,
                        AnnouncementRoleSelection.announcement_id == announcement.id,
                        AnnouncementRoleSelection.announcement_version_id == version.id,
                    )
                    .order_by(
                        desc(AnnouncementRoleSelection.created_at),
                        desc(AnnouncementRoleSelection.id),
                    )
                    .limit(1)
                )
                selected_role = latest_role.role_key if latest_role else None
            analysis_id = analysis.id
            answer_fingerprints = await _latest_semantic_answer_fingerprints(
                db,
                user_id=payload.user_id,
                announcement_version_id=payload.announcement_version_id,
            )
            semantic_evaluations = await _load_semantic_evaluations(
                db,
                analysis=analysis,
                company_profile_version_id=payload.company_profile_version_id,
                answer_fingerprints=answer_fingerprints,
            )

        async def publish(db: AsyncSession, _job) -> None:
            await serialize_decision_state(
                db,
                user_id=payload.user_id,
                announcement_id=payload.announcement_id,
            )
            current_announcement_version_id = await db.scalar(
                select(Announcement.current_version_id).where(
                    Announcement.id == payload.announcement_id
                )
            )
            current_profile_version_id = await db.scalar(
                select(CompanyProfile.current_version_id).where(
                    CompanyProfile.user_id == payload.user_id,
                    CompanyProfile.current_version_id == payload.company_profile_version_id,
                )
            )
            current_analysis_id = await db.scalar(
                select(AnalysisRun.id)
                .where(
                    AnalysisRun.announcement_version_id == payload.announcement_version_id,
                    AnalysisRun.status == "SUCCEEDED",
                )
                .order_by(desc(AnalysisRun.completed_at), desc(AnalysisRun.id))
                .limit(1)
            )
            current_role_row = await db.scalar(
                select(AnnouncementRoleSelection)
                .where(
                    AnnouncementRoleSelection.user_id == payload.user_id,
                    AnnouncementRoleSelection.announcement_id == payload.announcement_id,
                    AnnouncementRoleSelection.announcement_version_id
                    == payload.announcement_version_id,
                )
                .order_by(
                    desc(AnnouncementRoleSelection.created_at),
                    desc(AnnouncementRoleSelection.id),
                )
                .limit(1)
            )
            current_role = current_role_row.role_key if current_role_row else None
            current_answer_fingerprints = await _latest_semantic_answer_fingerprints(
                db,
                user_id=payload.user_id,
                announcement_version_id=payload.announcement_version_id,
            )
            if (
                current_announcement_version_id != payload.announcement_version_id
                or current_profile_version_id != payload.company_profile_version_id
                or current_analysis_id != analysis_id
                or current_role != selected_role
                or current_answer_fingerprints != answer_fingerprints
            ):
                return
            if semantic_evaluations is None:
                semantic_input_hash = semantic_input_fingerprint(
                    analysis_run_id=analysis_id,
                    answer_fingerprints=answer_fingerprints,
                    selected_role_key=selected_role,
                )
                await enqueue_job(
                    db,
                    "ANNOUNCEMENT_ANALYZE",
                    {
                        "announcementId": payload.announcement_id,
                        "announcementVersionId": payload.announcement_version_id,
                        "companyProfileVersionId": payload.company_profile_version_id,
                        "semanticInputHash": semantic_input_hash,
                        "semanticBaseAnalysisRunId": analysis_id,
                    },
                )
                return
            await publish_deterministic_decision(
                db,
                user_id=payload.user_id,
                announcement_id=payload.announcement_id,
                announcement_version_id=payload.announcement_version_id,
                company_profile_version_id=payload.company_profile_version_id,
                analysis_run_id=analysis_id,
                selected_role_key=selected_role,
                semantic_evaluations=semantic_evaluations,
            )

        return JobOutcome(publisher=publish)

    return handle


def announcement_analyze_handler(
    *,
    fixture_root: Path,
    source_storage_root: Path,
    analyzer: AnnouncementAnalyzer | None = None,
) -> JobHandler:
    root = fixture_root.resolve()

    async def handle(raw: dict[str, Any], context: JobContext) -> JobOutcome:
        try:
            payload = AnnouncementAnalyzePayload.model_validate(_normalize_payload(raw))
        except ValueError as exc:
            raise AIExecutionError(
                "ANALYSIS_JOB_PAYLOAD_INVALID", "Analysis job payload is invalid", retryable=False
            ) from exc
        if payload.fixture_manifest is not None:
            manifest_path = (root / payload.fixture_manifest).resolve()
            if not manifest_path.is_relative_to(root):
                raise AIExecutionError(
                    "FIXTURE_PATH_INVALID", "Fixture path is outside fixture root", retryable=False
                )

            async def publish(db: AsyncSession, _job) -> None:
                result = await persist_demo_fixture(
                    db, manifest_path, source_storage_root=source_storage_root
                )
                if payload.announcement_id and result.announcement_id != payload.announcement_id:
                    raise ValueError("Fixture announcement does not match requested announcement")

            return JobOutcome(publisher=publish)

        async def publish_failure(db: AsyncSession, job) -> None:
            if payload.announcement_id is None or payload.announcement_version_id is None:
                return
            await publish_analysis_failure(
                db,
                announcement_id=payload.announcement_id,
                announcement_version_id=payload.announcement_version_id,
                error_code=job.error_code or "ANALYSIS_FAILED",
                company_profile_version_id=payload.company_profile_version_id,
            )

        context.final_failure_publisher = publish_failure
        if analyzer is None:
            raise AIExecutionError(
                "AI_EXECUTOR_UNAVAILABLE",
                "Real announcement analyzer is not configured",
                retryable=False,
            )
        publisher = await analyzer.prepare(payload, context)
        return JobOutcome(publisher=publisher)

    return handle


def collection_handler(
    *,
    collector: AnnouncementCollector | None,
    required_scope: Literal["DAILY", "FULL"],
) -> JobHandler:
    async def handle(raw: dict[str, Any], context: JobContext) -> JobOutcome:
        if collector is None:
            raise AIExecutionError(
                "BIZINFO_COLLECTOR_UNAVAILABLE",
                "Bizinfo collector is not configured",
                retryable=False,
            )
        try:
            payload = CollectionPayload.model_validate(_normalize_payload(raw))
        except ValueError as exc:
            raise AIExecutionError(
                "COLLECTION_JOB_PAYLOAD_INVALID",
                "Collection job payload is invalid",
                retryable=False,
            ) from exc
        if payload.scope != required_scope:
            raise AIExecutionError(
                "COLLECTION_SCOPE_INVALID",
                "Collection job scope does not match its handler",
                retryable=False,
            )
        publisher = await collector.prepare(scope=payload.scope, context=context)
        return JobOutcome(publisher=publisher)

    return handle


def build_handler_registry(
    *,
    sessions: async_sessionmaker[AsyncSession],
    fixture_root: Path,
    source_storage_root: Path,
    analyzer: AnnouncementAnalyzer | None = None,
    collector: AnnouncementCollector | None = None,
) -> Mapping[str, JobHandler]:
    source_storage_root = source_storage_root.resolve()
    return {
        "DECISION_REEVALUATE": decision_reevaluate_handler(sessions),
        "ANNOUNCEMENT_ANALYZE": announcement_analyze_handler(
            fixture_root=fixture_root,
            source_storage_root=source_storage_root,
            analyzer=analyzer,
        ),
        "BIZINFO_COLLECT": collection_handler(collector=collector, required_scope="DAILY"),
        "BIZINFO_RECONCILE": collection_handler(collector=collector, required_scope="FULL"),
    }
