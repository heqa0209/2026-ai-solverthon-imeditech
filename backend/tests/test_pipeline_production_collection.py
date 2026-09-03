from __future__ import annotations

import asyncio
import hashlib
import stat
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select

from app.jobs import enqueue_job
from app.models import (
    AIStageRun,
    AnalysisRun,
    Announcement,
    AnnouncementVersion,
    CollectionSnapshot,
    Job,
    SourceFile,
)
from app.pipeline.ai import AIExecutionError, AIStage, FakeAIExecutor
from app.pipeline.attachments import MAX_FILE_BYTES
from app.pipeline.bizinfo import BIZINFO_ENDPOINT, BizinfoClient
from app.pipeline.collector import ProductionBizinfoCollector
from app.pipeline.handlers import build_handler_registry
from app.pipeline.jobs import JobQueue
from app.pipeline.scheduler import enqueue_due_collection_jobs
from app.pipeline.worker import Worker


def _wrapper(*, source_ids: tuple[str, ...], attachment_url: str | None = None) -> dict:
    items = []
    for source_id in source_ids:
        item = {
            "pblancId": source_id,
            "pblancNm": f"공고 {source_id}",
            "pblancUrl": f"https://example.test/{source_id}",
            "pblancCn": "지원 대상은 소기업입니다.",
            "creatPnttm": "20260903",
        }
        if attachment_url:
            item["attachments"] = [{"name": "notice.pdf", "url": attachment_url, "sizeBytes": 18}]
        items.append(item)
    return {"jsonArray": items, "totalCount": str(len(items))}


async def _publish(collector: ProductionBizinfoCollector, sessions, scope: str) -> None:
    outcome = await collector.prepare(scope=scope, context=SimpleNamespace())
    async with sessions.begin() as db:
        await outcome(db, SimpleNamespace())


async def _isolation_ok() -> bool:
    return True


def test_worker_registry_dispatches_bizinfo_collection_handler(
    session_factory, tmp_path: Path
) -> None:
    wrapper = _wrapper(source_ids=("A-1",))

    def transport(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=wrapper)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            collector = ProductionBizinfoCollector(
                sessions=session_factory,
                client=BizinfoClient("fixture-key", http),
                http=http,
                source_storage_root=tmp_path / "sources",
            )
            async with session_factory() as db:
                job = await enqueue_job(db, "BIZINFO_COLLECT", {"scope": "DAILY"})
                await db.commit()
                job_id = job.id
            worker = Worker(
                worker_id="collection-worker",
                queue=JobQueue(session_factory),
                handlers=build_handler_registry(
                    sessions=session_factory,
                    fixture_root=tmp_path,
                    source_storage_root=tmp_path / "sources",
                    collector=collector,
                ),
                isolation_check=_isolation_ok,
            )
            assert await worker.run_once() == 1
        async with session_factory() as db:
            completed = await db.get(Job, job_id)
            assert completed.status == "SUCCEEDED"
            assert await db.scalar(select(Announcement).where(Announcement.source_id == "A-1"))

    asyncio.run(scenario())


def test_scheduler_catches_up_daily_and_weekly_slots_once(session_factory) -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 3, 7, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        assert await enqueue_due_collection_jobs(session_factory, now=now) == 2
        assert await enqueue_due_collection_jobs(session_factory, now=now) == 0
        async with session_factory() as db:
            jobs = list(
                (
                    await db.scalars(
                        select(Job).where(
                            Job.job_type.in_(["BIZINFO_COLLECT", "BIZINFO_RECONCILE"])
                        )
                    )
                ).all()
            )
            assert {job.payload["scheduledFor"] for job in jobs} == {
                "2026-09-03T06:00:00+09:00",
                "2026-08-31T06:00:00+09:00",
            }

    asyncio.run(scenario())


def test_collection_preserves_wrapper_downloads_sources_and_only_enqueues_changed_versions(
    session_factory, tmp_path: Path
) -> None:
    pdf = b"%PDF-1.4\nfixture\n"
    wrapper = _wrapper(source_ids=("A-1",), attachment_url="https://files.test/notice.pdf")
    requests: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if str(request.url).startswith(BIZINFO_ENDPOINT):
            return httpx.Response(200, json=wrapper)
        return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            collector = ProductionBizinfoCollector(
                sessions=session_factory,
                client=BizinfoClient("fixture-key", http),
                http=http,
                source_storage_root=tmp_path / "sources",
            )
            await _publish(collector, session_factory, "DAILY")
            await _publish(collector, session_factory, "DAILY")

        async with session_factory() as db:
            announcement = await db.scalar(
                select(Announcement).where(Announcement.source_id == "A-1")
            )
            versions = await db.scalar(
                select(func.count())
                .select_from(AnnouncementVersion)
                .where(AnnouncementVersion.announcement_id == announcement.id)
            )
            source = await db.scalar(
                select(SourceFile).where(
                    SourceFile.announcement_version_id == announcement.current_version_id,
                    SourceFile.source_order == 1,
                )
            )
            analyze_jobs = await db.scalar(
                select(func.count(Job.id)).where(Job.job_type == "ANNOUNCEMENT_ANALYZE")
            )
            version = await db.get(AnnouncementVersion, announcement.current_version_id)
            assert versions == 1
            assert analyze_jobs == 1
            assert version.raw_payload == wrapper
            assert source.download_status == "SUCCEEDED"
            stored = (tmp_path / "sources" / source.storage_path).resolve()
            assert stored.is_relative_to((tmp_path / "sources").resolve())
            assert stored.read_bytes() == pdf
            assert source.sha256 == hashlib.sha256(pdf).hexdigest()
            assert stat.S_IMODE(stored.stat().st_mode) == 0o600

    asyncio.run(scenario())
    assert sum(url.startswith("https://files.test") for url in requests) == 2


def test_weekly_reconcile_requires_two_complete_successful_missing_snapshots(
    session_factory, tmp_path: Path
) -> None:
    responses = [
        _wrapper(source_ids=("A", "B")),
        _wrapper(source_ids=("A",)),
        _wrapper(source_ids=("A",)),
    ]

    def transport(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses.pop(0))

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            collector = ProductionBizinfoCollector(
                sessions=session_factory,
                client=BizinfoClient("fixture-key", http),
                http=http,
                source_storage_root=tmp_path / "sources",
            )
            await _publish(collector, session_factory, "FULL")
            await _publish(collector, session_factory, "FULL")
            async with session_factory() as db:
                missing = await db.scalar(select(Announcement).where(Announcement.source_id == "B"))
                assert missing.source_available is True
            await _publish(collector, session_factory, "FULL")

        async with session_factory() as db:
            missing = await db.scalar(select(Announcement).where(Announcement.source_id == "B"))
            snapshots = list(
                (
                    await db.scalars(
                        select(CollectionSnapshot).order_by(CollectionSnapshot.succeeded_at)
                    )
                ).all()
            )
            assert missing.source_available is False
            assert len(snapshots) == 3
            assert all(snapshot.complete for snapshot in snapshots)
            assert snapshots[-1].source_ids == ["A"]

    asyncio.run(scenario())


def test_streaming_download_retries_transient_failure(session_factory, tmp_path: Path) -> None:
    wrapper = _wrapper(source_ids=("A",), attachment_url="https://files.test/retry.pdf")
    attempts = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if str(request.url).startswith(BIZINFO_ENDPOINT):
            return httpx.Response(200, json=wrapper)
        attempts += 1
        if attempts < 3:
            return httpx.Response(503)
        return httpx.Response(200, content=b"%PDF-1.4\nfixture\n")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            collector = ProductionBizinfoCollector(
                sessions=session_factory,
                client=BizinfoClient("fixture-key", http),
                http=http,
                source_storage_root=tmp_path / "sources",
            )
            await _publish(collector, session_factory, "DAILY")
        async with session_factory() as db:
            attachment = await db.scalar(select(SourceFile).where(SourceFile.source_order == 1))
            assert attachment.download_status == "SUCCEEDED"

    asyncio.run(scenario())
    assert attempts == 3


def _selection_wrapper() -> dict:
    attachments = [
        {
            "name": f"attachment-{index}.pdf",
            "url": f"https://files.test/attachment-{index}.pdf",
            "sizeBytes": MAX_FILE_BYTES + 1 if index == 0 else 16 * 1024 * 1024,
        }
        for index in range(6)
    ]
    item = _wrapper(source_ids=("SELECT-1",))["jsonArray"][0]
    item["attachments"] = attachments
    return {"jsonArray": [item], "totalCount": "1"}


def test_collection_uses_luna_selection_before_possible_100mb_limit_and_records_reasons(
    session_factory, tmp_path: Path
) -> None:
    wrapper = _selection_wrapper()
    ranking = [
        {
            "source_order": index,
            "prioritized": index in {4, 5},
            "reason": f"자격조건 관련 우선순위 {index}",
        }
        for index in reversed(range(6))
    ]
    fake = FakeAIExecutor({AIStage.ATTACHMENT_SELECTION: {"ranked_attachments": ranking}})
    downloaded: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(BIZINFO_ENDPOINT):
            return httpx.Response(200, json=wrapper)
        downloaded.append(str(request.url))
        return httpx.Response(
            200,
            content=b"%PDF-1.4\nfixture\n",
            headers={"content-type": "application/pdf"},
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            collector = ProductionBizinfoCollector(
                sessions=session_factory,
                client=BizinfoClient("fixture-key", http),
                http=http,
                source_storage_root=tmp_path / "sources",
                executor=fake,
            )
            await _publish(collector, session_factory, "DAILY")

        async with session_factory() as db:
            stage = await db.scalar(
                select(AIStageRun).where(AIStageRun.stage == "ATTACHMENT_SELECTION")
            )
            analysis = await db.get(AnalysisRun, stage.analysis_run_id)
            output = stage.structured_output
            assert analysis.status == "PENDING"
            assert (stage.model, stage.effort) == ("gpt-5.6-luna", "low")
            assert [item["source_order"] for item in output["ranked_attachments"]] == [
                5,
                4,
                0,
                1,
                2,
                3,
            ]
            outcomes = {item["source_order"]: item for item in output["selection_outcomes"]}
            assert outcomes[0] == {
                "source_order": 0,
                "selected": False,
                "reason": "자격조건 관련 우선순위 0",
                "failure_code": "FILE_LIMIT_EXCEEDED",
            }
            assert all(outcomes[index]["selected"] for index in range(1, 6))

    asyncio.run(scenario())
    assert [url.rsplit("-", 1)[-1] for url in downloaded] == [
        "5.pdf",
        "4.pdf",
        "1.pdf",
        "2.pdf",
        "3.pdf",
    ]
    assert [invocation.stage for invocation in fake.invocations] == [AIStage.ATTACHMENT_SELECTION]


def test_collection_fails_safely_when_attachment_selection_model_is_unavailable(
    session_factory, tmp_path: Path
) -> None:
    wrapper = _selection_wrapper()
    fake = FakeAIExecutor({})
    attachment_requests = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal attachment_requests
        if str(request.url).startswith(BIZINFO_ENDPOINT):
            return httpx.Response(200, json=wrapper)
        attachment_requests += 1
        return httpx.Response(200, content=b"never downloaded")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            collector = ProductionBizinfoCollector(
                sessions=session_factory,
                client=BizinfoClient("fixture-key", http),
                http=http,
                source_storage_root=tmp_path / "sources",
                executor=fake,
            )
            with pytest.raises(AIExecutionError) as caught:
                await _publish(collector, session_factory, "DAILY")
            assert caught.value.code == "FIXTURE_OUTPUT_MISSING"
        async with session_factory() as db:
            assert await db.scalar(select(func.count(Announcement.id))) == 0

    asyncio.run(scenario())
    assert attachment_requests == 0
    assert len(fake.invocations) == 1
