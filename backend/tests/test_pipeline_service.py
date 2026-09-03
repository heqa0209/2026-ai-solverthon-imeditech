from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from app.models import Announcement, AnnouncementVersion, Job
from app.pipeline.service import DatabasePipelineCLIService


def test_reanalyze_uses_a_fresh_job_while_analyze_remains_idempotent(
    session_factory, tmp_path
) -> None:
    async def scenario() -> None:
        async with session_factory.begin() as db:
            announcement = Announcement(
                source_id="REANALYZE-1",
                source_url="https://example.test/reanalyze",
                source_available=True,
            )
            db.add(announcement)
            await db.flush()
            version = AnnouncementVersion(
                announcement_id=announcement.id,
                raw_payload={},
                content_hash="r" * 64,
                title="재분석 테스트",
            )
            db.add(version)
            await db.flush()
            announcement.current_version_id = version.id
            announcement_id = announcement.id

        service = DatabasePipelineCLIService(
            session_factory,
            fixture_root=tmp_path,
            source_storage_root=tmp_path / "sources",
        )
        assert await service.execute("announcement.analyze", announcement_id) == 1
        assert await service.execute("announcement.analyze", announcement_id) == 0
        assert await service.execute("announcement.reanalyze", announcement_id) == 1
        assert await service.execute("announcement.reanalyze", announcement_id) == 1

        async with session_factory() as db:
            count = await db.scalar(
                select(func.count(Job.id)).where(Job.job_type == "ANNOUNCEMENT_ANALYZE")
            )
            assert count == 3

    asyncio.run(scenario())
