from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.eligibility import Evaluation, evaluate_condition, evaluate_decision
from app.enums import ConditionStatus, Verdict
from app.models import (
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
)

ANALYSIS_FAILURE_EXPLANATION = "공고 분석을 완료하지 못해 원문 확인이 필요합니다."


def _decision_lock_key(user_id: str, announcement_id: str) -> int:
    digest = hashlib.blake2b(f"{user_id}\0{announcement_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


async def _serialize_publication(db: AsyncSession, *, user_id: str, announcement_id: str) -> None:
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(
            select(func.pg_advisory_xact_lock(_decision_lock_key(user_id, announcement_id)))
        )
        return
    await db.execute(
        select(Announcement.id).where(Announcement.id == announcement_id).with_for_update()
    )


async def publish_deterministic_decision(
    db: AsyncSession,
    *,
    user_id: str,
    announcement_id: str,
    announcement_version_id: str,
    company_profile_version_id: str,
    analysis_run_id: str,
    selected_role_key: str | None,
    explanation: str | None = None,
    semantic_evaluations: dict[str, Evaluation] | None = None,
    safety_unknown_condition_ids: set[str] | None = None,
) -> EligibilityDecision:
    """Compute and atomically publish a rules-owned eligibility decision.

    The caller owns the surrounding transaction. No AI output can override the
    returned deterministic comparisons.
    """
    profile_version = await db.get(CompanyProfileVersion, company_profile_version_id)
    analysis = await db.get(AnalysisRun, analysis_run_id)
    if profile_version is None or analysis is None or not analysis.canonical_ir:
        raise ValueError("decision inputs are unavailable")
    if analysis.announcement_version_id != announcement_version_id:
        raise ValueError("analysis run does not belong to the announcement version")

    profile = dict(profile_version.snapshot)
    extracted = list(
        (
            await db.scalars(
                select(ExtractedCondition).where(
                    ExtractedCondition.analysis_run_id == analysis_run_id
                )
            )
        ).all()
    )
    answers = list(
        (
            await db.scalars(
                select(AnnouncementAnswer).where(
                    AnnouncementAnswer.user_id == user_id,
                    AnnouncementAnswer.announcement_version_id == announcement_version_id,
                )
            )
        ).all()
    )
    latest_answers: dict[str, AnnouncementAnswer] = {}
    for answer in sorted(answers, key=lambda item: item.created_at):
        latest_answers[answer.condition_id] = answer

    condition_values = {
        condition.condition_key: latest_answers[condition.id].value
        for condition in extracted
        if condition.id in latest_answers
    }
    evaluated = evaluate_decision(
        analysis.canonical_ir,
        profile,
        selected_role_key,
        condition_values=condition_values,
        semantic_evaluations=semantic_evaluations,
        safety_unknown_condition_ids=safety_unknown_condition_ids,
    )

    await _serialize_publication(db, user_id=user_id, announcement_id=announcement_id)
    await db.execute(
        update(EligibilityDecision)
        .where(
            EligibilityDecision.user_id == user_id,
            EligibilityDecision.announcement_id == announcement_id,
            EligibilityDecision.is_current.is_(True),
        )
        .values(is_current=False)
    )
    decision = EligibilityDecision(
        user_id=user_id,
        announcement_id=announcement_id,
        announcement_version_id=announcement_version_id,
        company_profile_version_id=company_profile_version_id,
        selected_role_key=selected_role_key,
        calculated_verdict=evaluated.verdict,
        published_verdict=evaluated.verdict,
        decision_origin="CALCULATED",
        explanation=explanation,
        passed_track_key=evaluated.passed_track_key,
        is_current=True,
    )
    db.add(decision)
    await db.flush()

    ir_by_key = {
        str(item.get("condition_id") or item.get("conditionId")): item
        for item in analysis.canonical_ir.get("conditions", [])
        if isinstance(item, dict)
    }
    for condition in extracted:
        ir_condition: dict[str, Any] = ir_by_key.get(condition.condition_key, {})
        result = evaluated.conditions.get(condition.condition_key)
        if result is None:
            result = evaluate_condition(profile, ir_condition)
        db.add(
            ConditionResult(
                decision_id=decision.id,
                condition_id=condition.id,
                status=result.status,
                used_value=result.used_value,
                explanation=result.explanation,
                assumption_code=result.assumption_code,
                evidence=condition.evidence,
            )
        )
    await db.flush()
    return decision


async def publish_analysis_failure(
    db: AsyncSession,
    *,
    announcement_id: str,
    announcement_version_id: str,
    error_code: str,
    company_profile_version_id: str | None = None,
) -> int:
    """Record a final analysis failure and atomically publish safe current decisions.

    A stale analysis job is retained as history but must never replace decisions for
    a newer announcement version. The caller owns the transaction that also marks
    the job FAILED_FINAL.
    """
    announcement = await db.get(Announcement, announcement_id)
    version = await db.get(AnnouncementVersion, announcement_version_id)
    if announcement is None or version is None or version.announcement_id != announcement.id:
        return 0

    now = datetime.now(UTC)
    db.add(
        AnalysisRun(
            announcement_version_id=version.id,
            status="FAILED_FINAL",
            analysis_version="analysis-failed-v1",
            canonical_ir=None,
            started_at=now,
            completed_at=now,
            error_code=error_code,
        )
    )
    if announcement.current_version_id != version.id:
        return 0

    profiles = list(
        (
            await db.scalars(
                select(CompanyProfile).where(
                    CompanyProfile.current_version_id.is_not(None),
                    *(
                        [CompanyProfile.current_version_id == company_profile_version_id]
                        if company_profile_version_id is not None
                        else []
                    ),
                )
            )
        ).all()
    )
    published = 0
    for profile in profiles:
        profile_version_id = profile.current_version_id
        if profile_version_id is None:
            continue
        latest_role = await db.scalar(
            select(AnnouncementRoleSelection)
            .where(
                AnnouncementRoleSelection.user_id == profile.user_id,
                AnnouncementRoleSelection.announcement_id == announcement.id,
                AnnouncementRoleSelection.announcement_version_id == version.id,
            )
            .order_by(AnnouncementRoleSelection.created_at.desc())
            .limit(1)
        )
        await _serialize_publication(
            db,
            user_id=profile.user_id,
            announcement_id=announcement.id,
        )
        await db.execute(
            update(EligibilityDecision)
            .where(
                EligibilityDecision.user_id == profile.user_id,
                EligibilityDecision.announcement_id == announcement.id,
                EligibilityDecision.is_current.is_(True),
            )
            .values(is_current=False)
        )
        db.add(
            EligibilityDecision(
                user_id=profile.user_id,
                announcement_id=announcement.id,
                announcement_version_id=version.id,
                company_profile_version_id=profile_version_id,
                selected_role_key=latest_role.role_key if latest_role else None,
                calculated_verdict=None,
                published_verdict=Verdict.NEEDS_CONFIRMATION.value,
                decision_origin="SYSTEM_FAILURE",
                explanation=ANALYSIS_FAILURE_EXPLANATION,
                passed_track_key=None,
                is_current=True,
            )
        )
        published += 1
    await db.flush()
    return published


def system_failure_evaluation(message: str | None = None) -> Evaluation:
    return Evaluation(
        status=ConditionStatus.UNKNOWN,
        explanation=message or "결과 설명 생성에 실패해 원문 확인이 필요합니다.",
    )
