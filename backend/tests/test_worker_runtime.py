from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import Settings
from app.pipeline.analyzer import ProductionAnnouncementAnalyzer
from app.pipeline.collector import ProductionBizinfoCollector
from app.pipeline.processes import ProcessSupervisor
from app.worker import _build_runtime, _scheduler_loop


def _settings(tmp_path, *, api_key: str | None = "test-bizinfo-key") -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}",
        app_origin="http://testserver",
        session_secret="test-session-secret-that-is-at-least-32-bytes",
        bizinfo_api_key=api_key,
        source_storage_root=tmp_path / "sources",
        demo_fixture_root=tmp_path / "fixtures",
    )


@pytest.mark.asyncio
async def test_runtime_requires_nonempty_bizinfo_credential(tmp_path) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500))
    ) as http:
        for api_key in (None, "   "):
            with pytest.raises(RuntimeError, match="BIZINFO_API_KEY is required"):
                _build_runtime(
                    _settings(tmp_path, api_key=api_key),
                    http=http,
                    supervisor=ProcessSupervisor(),
                )


@pytest.mark.asyncio
async def test_runtime_injects_production_handlers(tmp_path) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500))
    ) as http:
        runtime = _build_runtime(
            _settings(tmp_path),
            http=http,
            supervisor=ProcessSupervisor(),
        )

    assert isinstance(runtime.collector, ProductionBizinfoCollector)
    assert isinstance(runtime.analyzer, ProductionAnnouncementAnalyzer)
    assert set(runtime.worker.handlers) == {
        "ANNOUNCEMENT_ANALYZE",
        "BIZINFO_COLLECT",
        "BIZINFO_RECONCILE",
        "DECISION_REEVALUATE",
    }


@pytest.mark.asyncio
async def test_scheduler_runs_immediately_and_on_each_tick(monkeypatch) -> None:
    calls = 0
    second_call = asyncio.Event()

    async def enqueue(_sessions) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            second_call.set()
        return 0

    monkeypatch.setattr("app.worker.enqueue_due_collection_jobs", enqueue)
    stop_event = asyncio.Event()
    task = asyncio.create_task(_scheduler_loop(stop_event, interval_seconds=0.001))
    await asyncio.wait_for(second_call.wait(), timeout=1)
    stop_event.set()
    await task

    assert calls == 2
