from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.enums import Verdict
from app.jobs import enqueue_job
from app.models import (
    Announcement,
    CompanyProfile,
    EligibilityDecision,
    Job,
    User,
)
from app.pipeline.ai import AI_STAGE_POLICIES, AIStage
from app.pipeline.cli import CommandPreview, PipelineCLIService
from app.pipeline.holdout import evaluate_holdout, load_holdout_manifest
from app.pipeline.jobs import JobQueue
from app.pipeline.persistence import persist_demo_fixture


class DatabasePipelineCLIService(PipelineCLIService):
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        fixture_root: Path,
    ):
        self.sessions = sessions
        self.fixture_root = fixture_root.resolve()
        self.queue = JobQueue(sessions)

    async def _announcement(self, db: AsyncSession, target_id: str | None) -> Announcement | None:
        if not target_id:
            return None
        return await db.get(Announcement, target_id)

    async def preview(self, operation: str, target_id: str | None) -> CommandPreview:
        async with self.sessions() as db:
            if operation in {"collect.run", "collect.reconcile"}:
                return CommandPreview(
                    target_id="bizinfo:daily" if operation.endswith("run") else "bizinfo:full",
                    target_version=None,
                    expected_count=1,
                )
            if operation == "job.retry":
                job = await db.get(Job, target_id) if target_id else None
                return CommandPreview(
                    target_id=target_id or "-",
                    target_version=None,
                    expected_count=int(job is not None and job.status.startswith("FAILED")),
                )
            announcement = await self._announcement(db, target_id)
            if announcement is None or announcement.current_version_id is None:
                return CommandPreview(
                    target_id=target_id or "-", target_version=None, expected_count=0
                )
            if operation == "decision.reevaluate":
                profiles = list(
                    (
                        await db.scalars(
                            select(CompanyProfile).where(
                                CompanyProfile.current_version_id.is_not(None)
                            )
                        )
                    ).all()
                )
                return CommandPreview(
                    target_id=announcement.id,
                    target_version=announcement.current_version_id,
                    expected_count=len(profiles),
                )
            policy = AI_STAGE_POLICIES[AIStage.CONDITION_EXTRACTION]
            return CommandPreview(
                target_id=announcement.id,
                target_version=announcement.current_version_id,
                expected_count=1,
                model_efforts=(f"{policy.model}:{policy.effort}",),
            )

    async def execute(self, operation: str, target_id: str | None) -> int:
        async with self.sessions() as db:
            if operation in {"collect.run", "collect.reconcile"}:
                job_type = "BIZINFO_COLLECT" if operation == "collect.run" else "BIZINFO_RECONCILE"
                scope = "DAILY" if operation.endswith("run") else "FULL"
                await enqueue_job(db, job_type, {"scope": scope})
                await db.commit()
                return 1
            announcement = await self._announcement(db, target_id)
            if announcement is None or announcement.current_version_id is None:
                return 0
            if operation in {"announcement.analyze", "announcement.reanalyze"}:
                await enqueue_job(
                    db,
                    "ANNOUNCEMENT_ANALYZE",
                    {
                        "announcementId": announcement.id,
                        "announcementVersionId": announcement.current_version_id,
                    },
                )
                await db.commit()
                return 1
            if operation == "decision.reevaluate":
                profiles = list(
                    (
                        await db.scalars(
                            select(CompanyProfile).where(
                                CompanyProfile.current_version_id.is_not(None)
                            )
                        )
                    ).all()
                )
                for profile in profiles:
                    await enqueue_job(
                        db,
                        "DECISION_REEVALUATE",
                        {
                            "userId": profile.user_id,
                            "announcementId": announcement.id,
                            "announcementVersionId": announcement.current_version_id,
                            "companyProfileVersionId": profile.current_version_id,
                            "cause": "CLI_REQUESTED",
                        },
                    )
                await db.commit()
                return len(profiles)
            return 0

    async def job_status(self, job_id: str) -> str:
        async with self.sessions() as db:
            job = await db.get(Job, job_id)
            if job is None:
                return f"job={job_id} status=NOT_FOUND"
            return (
                f"job={job.id} type={job.job_type} status={job.status} "
                f"attempt={job.attempt}/{job.max_attempts} errorCode={job.error_code or '-'}"
            )

    async def retry_job(self, job_id: str) -> bool:
        return await self.queue.retry(job_id=job_id)

    async def load_fixture(self, manifest_path: Path) -> int:
        async with self.sessions.begin() as db:
            result = await persist_demo_fixture(db, manifest_path)
            return result.processed

    async def run_holdout(self, manifest_path: Path) -> tuple[int, bool]:
        manifest = load_holdout_manifest(manifest_path)
        actual: dict[str, Verdict] = {}
        async with self.sessions() as db:
            for case in manifest.cases:
                profile_data = json.loads(
                    (manifest_path.parent / case.company_profile_path).read_text(encoding="utf-8")
                )
                announcement_data = json.loads(
                    (manifest_path.parent / case.announcement_path).read_text(encoding="utf-8")
                )
                username = profile_data.get("username")
                source_id = announcement_data.get("sourceId")
                decision = await db.scalar(
                    select(EligibilityDecision)
                    .join(User, User.id == EligibilityDecision.user_id)
                    .join(Announcement, Announcement.id == EligibilityDecision.announcement_id)
                    .where(
                        User.username == username,
                        Announcement.source_id == source_id,
                        EligibilityDecision.is_current.is_(True),
                    )
                    .order_by(desc(EligibilityDecision.created_at))
                    .limit(1)
                )
                if decision is not None:
                    actual[case.case_id] = Verdict(decision.published_verdict)
        report = evaluate_holdout(manifest, actual)
        return report.sample_size, report.passed
