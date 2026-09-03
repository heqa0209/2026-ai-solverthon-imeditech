from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.decision_service import publish_deterministic_decision
from app.models import (
    AnalysisRun,
    Announcement,
    AnnouncementRoleSelection,
    AnnouncementVersion,
    CompanyProfileVersion,
)
from app.pipeline.ai import AIExecutionError
from app.pipeline.jobs import Publisher
from app.pipeline.persistence import persist_demo_fixture
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


class AnnouncementAnalyzer(Protocol):
    async def prepare(
        self, payload: AnnouncementAnalyzePayload, context: JobContext
    ) -> Publisher: ...


def _normalize_payload(raw: dict[str, Any]) -> dict[str, Any]:
    aliases = {
        "userId": "user_id",
        "announcementId": "announcement_id",
        "announcementVersionId": "announcement_version_id",
        "companyProfileVersionId": "company_profile_version_id",
        "selectedRoleKey": "selected_role_key",
        "fixtureManifest": "fixture_manifest",
    }
    return {aliases.get(key, key): value for key, value in raw.items()}


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
                .order_by(desc(AnalysisRun.completed_at))
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
                    .order_by(desc(AnnouncementRoleSelection.created_at))
                    .limit(1)
                )
                selected_role = latest_role.role_key if latest_role else None
            analysis_id = analysis.id

        async def publish(db: AsyncSession, _job) -> None:
            await publish_deterministic_decision(
                db,
                user_id=payload.user_id,
                announcement_id=payload.announcement_id,
                announcement_version_id=payload.announcement_version_id,
                company_profile_version_id=payload.company_profile_version_id,
                analysis_run_id=analysis_id,
                selected_role_key=selected_role,
            )

        return JobOutcome(publisher=publish)

    return handle


def announcement_analyze_handler(
    *,
    fixture_root: Path,
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
                result = await persist_demo_fixture(db, manifest_path)
                if payload.announcement_id and result.announcement_id != payload.announcement_id:
                    raise ValueError("Fixture announcement does not match requested announcement")

            return JobOutcome(publisher=publish)
        if analyzer is None:
            raise AIExecutionError(
                "AI_EXECUTOR_UNAVAILABLE",
                "Real announcement analyzer is not configured",
                retryable=False,
            )
        publisher = await analyzer.prepare(payload, context)
        return JobOutcome(publisher=publisher)

    return handle


def unsupported_job_handler(code: str) -> JobHandler:
    async def handle(_payload: dict[str, Any], _context: JobContext) -> JobOutcome:
        raise AIExecutionError(code, "This worker operation is not configured", retryable=False)

    return handle


def build_handler_registry(
    *,
    sessions: async_sessionmaker[AsyncSession],
    fixture_root: Path,
    analyzer: AnnouncementAnalyzer | None = None,
) -> Mapping[str, JobHandler]:
    return {
        "DECISION_REEVALUATE": decision_reevaluate_handler(sessions),
        "ANNOUNCEMENT_ANALYZE": announcement_analyze_handler(
            fixture_root=fixture_root, analyzer=analyzer
        ),
        "BIZINFO_COLLECT": unsupported_job_handler("BIZINFO_COLLECTOR_UNAVAILABLE"),
        "BIZINFO_RECONCILE": unsupported_job_handler("BIZINFO_RECONCILER_UNAVAILABLE"),
    }
