from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.eligibility import Evaluation, evaluate_condition, evaluate_decision
from app.enums import ConditionStatus
from app.models import (
    AnalysisRun,
    AnnouncementAnswer,
    CompanyProfileVersion,
    ConditionResult,
    EligibilityDecision,
    ExtractedCondition,
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
    )

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


def system_failure_evaluation(message: str | None = None) -> Evaluation:
    return Evaluation(
        status=ConditionStatus.UNKNOWN,
        explanation=message or "결과 설명 생성에 실패해 원문 확인이 필요합니다.",
    )
