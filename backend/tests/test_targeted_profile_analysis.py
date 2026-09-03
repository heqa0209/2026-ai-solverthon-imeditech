from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import (
    AIStageRun,
    Announcement,
    AnnouncementVersion,
    CompanyProfile,
    CompanyProfileVersion,
    EligibilityDecision,
    SourceFile,
    User,
)
from app.pipeline.ai import AIExecutionError, AIStage, FakeAIExecutor
from app.pipeline.analyzer import ProductionAnnouncementAnalyzer


async def _seed_two_profiles(session_factory, storage: Path) -> dict[str, str]:
    text = "지원 대상은 소기업입니다."
    body = text.encode()
    body_hash = hashlib.sha256(body).hexdigest()
    async with session_factory.begin() as db:
        first_user = await db.scalar(select(User).where(User.username == "demo.user"))
        second_user = User(username="second.user", password_hash="test-only")
        db.add(second_user)
        await db.flush()

        versions: list[CompanyProfileVersion] = []
        for index, (user, scale) in enumerate(
            ((first_user, "SMALL"), (second_user, "LARGE")), start=1
        ):
            profile = CompanyProfile(user_id=user.id)
            db.add(profile)
            await db.flush()
            version = CompanyProfileVersion(
                profile_id=profile.id,
                user_id=user.id,
                version=1,
                snapshot={"companyName": f"합성 기업 {index}", "companyScale": scale},
                raw_input={"companyName": f"합성 기업 {index}", "companyScale": scale},
            )
            db.add(version)
            await db.flush()
            profile.current_version_id = version.id
            versions.append(version)

        announcement = Announcement(
            source_id="TARGET-PROFILE-1",
            source_url="https://example.test/target-profile",
            source_available=True,
        )
        db.add(announcement)
        await db.flush()
        announcement_version = AnnouncementVersion(
            announcement_id=announcement.id,
            raw_payload={"jsonArray": []},
            content_hash="t" * 64,
            title="대상 profile 분석 공고",
            body_text=text,
        )
        db.add(announcement_version)
        await db.flush()
        announcement.current_version_id = announcement_version.id
        relative = Path(announcement_version.id) / "body.txt"
        target = storage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        source = SourceFile(
            announcement_version_id=announcement_version.id,
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
        await db.flush()
        return {
            "first_user": first_user.id,
            "first_profile_version": versions[0].id,
            "second_profile_version": versions[1].id,
            "announcement": announcement.id,
            "announcement_version": announcement_version.id,
            "source": source.id,
            "source_hash": body_hash,
            "text": text,
        }


def _ir(seeded: dict[str, str]) -> dict:
    return {
        "analysis_version": "analysis-v1",
        "summary": "소기업 대상",
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
                        "source_file_id": seeded["source"],
                        "source_version": seeded["source_hash"],
                        "page": None,
                        "verbatim_text": seeded["text"],
                        "source_priority": 10,
                    }
                ],
            }
        ],
        "questions": [],
    }


def test_targeted_analysis_transmits_and_publishes_only_requested_current_profile(
    session_factory, tmp_path: Path
) -> None:
    async def scenario() -> None:
        storage = tmp_path / "sources"
        seeded = await _seed_two_profiles(session_factory, storage)
        fake = FakeAIExecutor(
            {
                AIStage.CONDITION_EXTRACTION: _ir(seeded),
                AIStage.USER_EXPLANATION: {"explanation": "소기업 조건을 충족합니다."},
            }
        )
        analyzer = ProductionAnnouncementAnalyzer(
            sessions=session_factory,
            executor=fake,
            source_storage_root=storage,
        )
        publisher = await analyzer.prepare(
            type(
                "Payload",
                (),
                {
                    "announcement_id": seeded["announcement"],
                    "announcement_version_id": seeded["announcement_version"],
                    "company_profile_version_id": seeded["first_profile_version"],
                },
            )(),
            type("Context", (), {})(),
        )
        async with session_factory.begin() as db:
            await publisher(db, type("Job", (), {})())

        async with session_factory() as db:
            decisions = list((await db.scalars(select(EligibilityDecision))).all())
            scoped_stages = list(
                (
                    await db.scalars(
                        select(AIStageRun).where(AIStageRun.company_profile_version_id.is_not(None))
                    )
                ).all()
            )
            assert len(decisions) == 1
            assert decisions[0].user_id == seeded["first_user"]
            assert decisions[0].company_profile_version_id == seeded["first_profile_version"]
            assert decisions[0].published_verdict == "ELIGIBLE"
            assert {stage.company_profile_version_id for stage in scoped_stages} == {
                seeded["first_profile_version"]
            }
            explanation_call = next(
                item for item in fake.invocations if item.stage == AIStage.USER_EXPLANATION
            )
            transmitted = explanation_call.stdin.decode()
            assert seeded["first_profile_version"] in transmitted
            assert seeded["second_profile_version"] not in transmitted
            assert "합성 기업 1" in transmitted
            assert "합성 기업 2" not in transmitted

    asyncio.run(scenario())


def test_targeted_analysis_rejects_non_current_profile_without_model_call(
    session_factory, tmp_path: Path
) -> None:
    async def scenario() -> None:
        storage = tmp_path / "sources"
        seeded = await _seed_two_profiles(session_factory, storage)
        async with session_factory.begin() as db:
            old_version = await db.get(CompanyProfileVersion, seeded["first_profile_version"])
            profile = await db.get(CompanyProfile, old_version.profile_id)
            current = CompanyProfileVersion(
                profile_id=profile.id,
                user_id=old_version.user_id,
                version=2,
                snapshot={"companyName": "최신 기업", "companyScale": "SMALL"},
                raw_input={"companyName": "최신 기업", "companyScale": "SMALL"},
            )
            db.add(current)
            await db.flush()
            profile.current_version_id = current.id

        fake = FakeAIExecutor({})
        analyzer = ProductionAnnouncementAnalyzer(
            sessions=session_factory,
            executor=fake,
            source_storage_root=storage,
        )
        with pytest.raises(AIExecutionError) as caught:
            await analyzer.prepare(
                type(
                    "Payload",
                    (),
                    {
                        "announcement_id": seeded["announcement"],
                        "announcement_version_id": seeded["announcement_version"],
                        "company_profile_version_id": seeded["first_profile_version"],
                    },
                )(),
                type("Context", (), {})(),
            )
        assert caught.value.code == "ANALYSIS_PROFILE_VERSION_INVALID"
        assert fake.invocations == []

    asyncio.run(scenario())
