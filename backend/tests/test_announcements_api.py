from __future__ import annotations

import asyncio
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import (
    AnalysisRun,
    Announcement,
    AnnouncementVersion,
    CompanyProfileVersion,
    ConditionResult,
    EligibilityDecision,
    ExtractedCondition,
    Job,
    SourceFile,
    User,
)


def headers(csrf: str) -> dict[str, str]:
    return {"Origin": "http://testserver", "X-CSRF-Token": csrf}


def save_company(client: TestClient, csrf: str) -> None:
    response = client.put(
        "/api/v1/company",
        json={"companyName": "아이메디텍", "companyScale": "SMALL"},
        headers={**headers(csrf), "If-Match": '"0"'},
    )
    assert response.status_code == 200


async def seed_announcement(session_factory, *, decision_user: str = "demo.user") -> dict[str, str]:
    async with session_factory() as db:
        user = await db.scalar(select(User).where(User.username == decision_user))
        profile_version = await db.scalar(
            select(CompanyProfileVersion)
            .where(CompanyProfileVersion.user_id == user.id)
            .order_by(CompanyProfileVersion.version.desc())
        )
        announcement = Announcement(
            source_id="BIZ-2026-1",
            source_url="https://example.test/original",
            source_available=True,
        )
        db.add(announcement)
        await db.flush()
        version = AnnouncementVersion(
            announcement_id=announcement.id,
            raw_payload={"wrapper": {"id": "BIZ-2026-1"}},
            content_hash="a" * 64,
            title="의료기기 기술개발 지원사업",
            agency_name="중소벤처기업부",
            summary_text="의료기기 기업의 기술개발을 지원합니다.",
            body_text="소기업이어야 합니다.",
            published_on=date(2026, 9, 1),
            recruitment_starts_on=date(2026, 9, 1),
            recruitment_ends_on=date(2099, 9, 30),
        )
        db.add(version)
        await db.flush()
        announcement.current_version_id = version.id
        analysis = AnalysisRun(
            announcement_version_id=version.id,
            status="SUCCEEDED",
            analysis_version="v1",
            canonical_ir={
                "roles": [
                    {"role_key": "LEAD", "label": "주관기관"},
                    {"role_key": "PARTNER", "label": "참여기관"},
                ],
                "groups": [
                    {
                        "group_id": "lead",
                        "parent_group_id": None,
                        "operator": "ALL",
                        "role_keys": ["LEAD"],
                    },
                    {
                        "group_id": "partner",
                        "parent_group_id": None,
                        "operator": "ALL",
                        "role_keys": ["PARTNER"],
                    },
                ],
                "conditions": [
                    {
                        "condition_id": "lead-scale",
                        "group_id": "lead",
                        "kind": "MANDATORY",
                        "subject": "COMPANY_SCALE",
                        "operator": "EQ",
                        "expected_value": {"type": "ENUM", "value": "SMALL"},
                    },
                    {
                        "condition_id": "partner-scale",
                        "group_id": "partner",
                        "kind": "MANDATORY",
                        "subject": "COMPANY_SCALE",
                        "operator": "EQ",
                        "expected_value": {"type": "ENUM", "value": "MEDIUM"},
                    },
                ],
                "questions": [
                    {
                        "condition_id": "scale-condition",
                        "prompt": "기업규모를 확인해 주세요.",
                        "answer_type": "INTEGER",
                        "options": None,
                        "unit": "명",
                        "evidence": [
                            {
                                "source_file_id": None,
                                "source_version": "fixture-v1",
                                "page": None,
                                "verbatim_text": "소기업이어야 합니다.",
                                "source_priority": 10,
                            }
                        ],
                    }
                ],
            },
        )
        db.add(analysis)
        await db.flush()
        condition = ExtractedCondition(
            analysis_run_id=analysis.id,
            condition_key="scale-condition",
            group_key="root",
            track_key="TRACK-A",
            role_key="LEAD",
            kind="MANDATORY",
            subject="COMPANY_SCALE",
            operator="EQ",
            expected_value={"type": "ENUM", "value": "SMALL"},
            evidence=[
                {
                    "source_file_id": None,
                    "source_version": version.id,
                    "verbatim_text": "소기업이어야 합니다.",
                    "source_priority": 10,
                }
            ],
        )
        db.add(condition)
        await db.flush()
        decision = EligibilityDecision(
            user_id=user.id,
            announcement_id=announcement.id,
            announcement_version_id=version.id,
            company_profile_version_id=profile_version.id,
            selected_role_key=None,
            calculated_verdict="ELIGIBLE",
            published_verdict="ELIGIBLE",
            explanation="소기업 조건을 충족합니다.",
            passed_track_key="TRACK-A",
            is_current=True,
        )
        db.add(decision)
        await db.flush()
        db.add(
            ConditionResult(
                decision_id=decision.id,
                condition_id=condition.id,
                status="PASS",
                used_value={"value": "SMALL"},
                explanation="소기업입니다.",
                evidence=condition.evidence,
            )
        )
        source_file = SourceFile(
            announcement_version_id=version.id,
            name="공고문.pdf",
            source_url="https://example.test/file.pdf",
            source_order=1,
            source_priority=10,
            download_status="FAILED_FINAL",
            extraction_status="SKIPPED",
            failure_code="FIXTURE_NOT_DOWNLOADED",
        )
        db.add(source_file)
        await db.commit()
        return {
            "announcement": announcement.id,
            "version": version.id,
            "condition": condition.id,
            "file": source_file.id,
            "decision": decision.id,
            "decision_published_at": decision.created_at.isoformat(),
        }


def test_list_detail_interest_and_attachment_authorization(
    authenticated_client: tuple[TestClient, str], session_factory
) -> None:
    client, csrf = authenticated_client
    save_company(client, csrf)
    seeded = asyncio.run(seed_announcement(session_factory))

    page = client.get("/api/v1/announcements")
    assert page.status_code == 200
    assert page.json()["pageSize"] == 10
    assert page.json()["total"] == 1
    assert page.json()["items"][0]["eligibility"] == "ELIGIBLE"
    assert page.json()["items"][0]["recruitmentStatus"] == "OPEN"
    assert page.json()["items"][0]["decisionId"] == seeded["decision"]
    assert page.json()["items"][0]["decisionPublishedAt"] == seeded["decision_published_at"]

    detail = client.get(f"/api/v1/announcements/{seeded['announcement']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["conditions"][0]["status"] == "PASS"
    assert body["decisionId"] == seeded["decision"]
    assert body["decisionPublishedAt"] == seeded["decision_published_at"]
    assert body["conditions"][0]["evidence"][0]["verbatimText"] == "소기업이어야 합니다."
    assert body["files"][0]["failureCode"] == "FIXTURE_NOT_DOWNLOADED"
    assert body["rolePredictions"] == [
        {"roleKey": "LEAD", "label": "주관기관", "eligibility": "ELIGIBLE"},
        {"roleKey": "PARTNER", "label": "참여기관", "eligibility": "INELIGIBLE"},
    ]
    assert body["questions"] == [
        {
            "conditionId": seeded["condition"],
            "prompt": "기업규모를 확인해 주세요.",
            "valueType": "INTEGER",
            "options": None,
            "unit": "명",
            "evidence": [
                {
                    "sourceFileId": None,
                    "sourceVersion": "fixture-v1",
                    "sourceName": None,
                    "page": None,
                    "verbatimText": "소기업이어야 합니다.",
                    "sourcePriority": 10,
                }
            ],
        }
    ]
    assert client.get("/api/v1/announcements?interestStatus=ANY_SET").json()["total"] == 0

    interested = client.put(
        f"/api/v1/announcements/{seeded['announcement']}/interest",
        json={"status": "INTERESTED"},
        headers=headers(csrf),
    )
    assert interested.status_code == 200
    assert interested.json()["status"] == "INTERESTED"
    filtered = client.get("/api/v1/announcements?interestStatus=INTERESTED")
    assert filtered.json()["total"] == 1
    any_configured = client.get("/api/v1/announcements?interestStatus=ANY_SET")
    assert any_configured.status_code == 200
    assert any_configured.json()["total"] == 1
    missing_file = client.get(
        f"/api/v1/announcements/{seeded['announcement']}/files/{seeded['file']}"
    )
    assert missing_file.status_code == 404


def test_role_answer_and_reevaluate_are_idempotent_and_version_checked(
    authenticated_client: tuple[TestClient, str], session_factory
) -> None:
    client, csrf = authenticated_client
    save_company(client, csrf)
    seeded = asyncio.run(seed_announcement(session_factory))
    role_path = f"/api/v1/announcements/{seeded['announcement']}/role"
    payload = {"announcementVersionId": seeded["version"], "roleKey": "LEAD"}
    first = client.put(role_path, json=payload, headers=headers(csrf))
    second = client.put(role_path, json=payload, headers=headers(csrf))
    assert first.status_code == second.status_code == 202
    assert first.json()["requestId"] == second.json()["requestId"]

    answer_path = f"/api/v1/announcements/{seeded['announcement']}/answers"
    invalid_answer = client.post(
        answer_path,
        json={
            "announcementVersionId": seeded["version"],
            "conditionId": seeded["condition"],
            "value": "7",
            "source": "USER_VERIFIED",
        },
        headers=headers(csrf),
    )
    assert invalid_answer.status_code == 422
    assert invalid_answer.json()["code"] == "ANSWER_TYPE_INVALID"
    answer = {
        "announcementVersionId": seeded["version"],
        "conditionId": seeded["condition"],
        "value": 7,
        "source": "USER_VERIFIED",
    }
    answer_first = client.post(answer_path, json=answer, headers=headers(csrf))
    answer_second = client.post(answer_path, json=answer, headers=headers(csrf))
    assert answer_first.status_code == answer_second.status_code == 202
    assert answer_first.json()["requestId"] == answer_second.json()["requestId"]

    reevaluate_path = f"/api/v1/announcements/{seeded['announcement']}/reevaluate"
    reevaluate = client.post(
        reevaluate_path,
        json={"announcementVersionId": seeded["version"]},
        headers=headers(csrf),
    )
    assert reevaluate.status_code == 202
    stale = client.post(
        reevaluate_path,
        json={"announcementVersionId": "stale-version"},
        headers=headers(csrf),
    )
    assert stale.status_code == 409

    async def job_count() -> int:
        async with session_factory() as db:
            return await db.scalar(select(func.count(Job.id))) or 0

    assert asyncio.run(job_count()) == 3


def test_non_owner_decision_does_not_expose_resource(
    authenticated_client: tuple[TestClient, str], session_factory
) -> None:
    client, csrf = authenticated_client
    save_company(client, csrf)

    async def create_other() -> None:
        from app.auth import create_user
        from app.models import CompanyProfile, CompanyProfileVersion

        async with session_factory() as db:
            other = await create_user(db, "other.user", "secret")
            profile = CompanyProfile(user_id=other.id)
            db.add(profile)
            await db.flush()
            version = CompanyProfileVersion(
                profile_id=profile.id,
                user_id=other.id,
                version=1,
                snapshot={"companyName": "다른 기업"},
                raw_input={"companyName": "다른 기업"},
            )
            db.add(version)
            await db.flush()
            profile.current_version_id = version.id
            await db.commit()

    asyncio.run(create_other())
    seeded = asyncio.run(seed_announcement(session_factory, decision_user="other.user"))
    response = client.get(f"/api/v1/announcements/{seeded['announcement']}")
    assert response.status_code == 404
