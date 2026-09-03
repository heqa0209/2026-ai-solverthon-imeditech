from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.decision_service import publish_deterministic_decision
from app.models import (
    AnalysisRun,
    Announcement,
    AnnouncementAnswer,
    AnnouncementVersion,
    CompanyProfile,
    CompanyProfileVersion,
    ConditionResult,
    EligibilityDecision,
    ExtractedCondition,
    User,
)


def test_publish_decision_replaces_pointer_and_applies_condition_scoped_answer(
    session_factory,
) -> None:
    async def scenario() -> None:
        async with session_factory() as db:
            user = await db.scalar(select(User).where(User.username == "demo.user"))
            profile = CompanyProfile(user_id=user.id)
            db.add(profile)
            await db.flush()
            profile_version = CompanyProfileVersion(
                profile_id=profile.id,
                user_id=user.id,
                version=1,
                snapshot={"companyName": "아이메디텍", "employeeCount": 11},
                raw_input={"companyName": "아이메디텍", "employeeCount": 11},
            )
            db.add(profile_version)
            await db.flush()
            profile.current_version_id = profile_version.id
            announcement = Announcement(
                source_id="decision-test", source_url="https://example.test"
            )
            db.add(announcement)
            await db.flush()
            version = AnnouncementVersion(
                announcement_id=announcement.id,
                raw_payload={},
                content_hash="d" * 64,
                title="고용 인원 조건",
            )
            db.add(version)
            await db.flush()
            announcement.current_version_id = version.id
            analysis = AnalysisRun(
                announcement_version_id=version.id,
                status="SUCCEEDED",
                analysis_version="v1",
                canonical_ir={
                    "groups": [{"group_id": "root", "operator": "ALL"}],
                    "conditions": [
                        {
                            "condition_id": "employee-limit",
                            "group_id": "root",
                            "kind": "MANDATORY",
                            "subject": "EMPLOYEE_COUNT",
                            "operator": "LTE",
                            "expected_value": {"type": "INTEGER", "value": 10},
                        }
                    ],
                },
            )
            db.add(analysis)
            await db.flush()
            condition = ExtractedCondition(
                analysis_run_id=analysis.id,
                condition_key="employee-limit",
                group_key="root",
                kind="MANDATORY",
                subject="EMPLOYEE_COUNT",
                operator="LTE",
                expected_value={"type": "INTEGER", "value": 10},
                evidence=[{"verbatim_text": "상시근로자 10인 이하"}],
            )
            db.add(condition)
            await db.flush()
            first = await publish_deterministic_decision(
                db,
                user_id=user.id,
                announcement_id=announcement.id,
                announcement_version_id=version.id,
                company_profile_version_id=profile_version.id,
                analysis_run_id=analysis.id,
                selected_role_key=None,
            )
            assert first.published_verdict == "INELIGIBLE"
            db.add(
                AnnouncementAnswer(
                    user_id=user.id,
                    announcement_version_id=version.id,
                    condition_id=condition.id,
                    value=9,
                    source="OFFICIAL_DOCUMENT",
                )
            )
            await db.flush()
            second = await publish_deterministic_decision(
                db,
                user_id=user.id,
                announcement_id=announcement.id,
                announcement_version_id=version.id,
                company_profile_version_id=profile_version.id,
                analysis_run_id=analysis.id,
                selected_role_key=None,
            )
            await db.commit()
            assert second.published_verdict == "ELIGIBLE"
            decisions = list(
                (
                    await db.scalars(
                        select(EligibilityDecision).order_by(EligibilityDecision.created_at)
                    )
                ).all()
            )
            assert [decision.is_current for decision in decisions] == [False, True]
            result = await db.scalar(
                select(ConditionResult).where(ConditionResult.decision_id == second.id)
            )
            assert result.status == "PASS"

    asyncio.run(scenario())
