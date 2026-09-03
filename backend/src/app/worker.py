from __future__ import annotations

import asyncio
import os
import signal
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import Settings, get_settings
from app.db import SessionFactory, engine
from app.pipeline.ai import CodexExecutor
from app.pipeline.analyzer import ProductionAnnouncementAnalyzer
from app.pipeline.bizinfo import BizinfoClient
from app.pipeline.collector import ProductionBizinfoCollector
from app.pipeline.handlers import build_handler_registry
from app.pipeline.isolation import run_ai_runner_isolation_self_test
from app.pipeline.jobs import JobQueue
from app.pipeline.processes import ProcessSupervisor
from app.pipeline.scheduler import enqueue_due_collection_jobs
from app.pipeline.worker import Worker

SCHEDULER_TICK_SECONDS = 60.0


@dataclass(frozen=True)
class WorkerRuntime:
    worker: Worker
    supervisor: ProcessSupervisor
    collector: ProductionBizinfoCollector
    analyzer: ProductionAnnouncementAnalyzer


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _build_runtime(
    settings: Settings,
    *,
    http: httpx.AsyncClient,
    supervisor: ProcessSupervisor,
) -> WorkerRuntime:
    api_key = settings.bizinfo_api_key.strip() if settings.bizinfo_api_key else ""
    if not api_key:
        raise RuntimeError("BIZINFO_API_KEY is required before the worker can start")

    collector = ProductionBizinfoCollector(
        sessions=SessionFactory,
        client=BizinfoClient(api_key, http),
        http=http,
        source_storage_root=settings.source_storage_root,
        executor=CodexExecutor(
            supervisor,
            timeout_seconds=settings.ai_stage_timeout_seconds,
        ),
    )
    analyzer = ProductionAnnouncementAnalyzer(
        sessions=SessionFactory,
        executor=CodexExecutor(
            supervisor,
            timeout_seconds=settings.ai_stage_timeout_seconds,
        ),
        source_storage_root=settings.source_storage_root,
    )
    temp_root = Path(tempfile.gettempdir()) / "solverthon-ai-worker"

    async def isolation_check() -> bool:
        return await asyncio.to_thread(run_ai_runner_isolation_self_test, temp_root)

    worker = Worker(
        worker_id=_worker_id(),
        queue=JobQueue(SessionFactory),
        handlers=build_handler_registry(
            sessions=SessionFactory,
            fixture_root=settings.demo_fixture_root,
            source_storage_root=settings.source_storage_root,
            analyzer=analyzer,
            collector=collector,
        ),
        concurrency=settings.ai_max_concurrency,
        heartbeat_seconds=30,
        isolation_check=isolation_check,
    )
    return WorkerRuntime(worker, supervisor, collector, analyzer)


async def _scheduler_loop(
    stop_event: asyncio.Event,
    *,
    interval_seconds: float = SCHEDULER_TICK_SECONDS,
) -> None:
    while not stop_event.is_set():
        await enqueue_due_collection_jobs(SessionFactory)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


async def run() -> None:
    settings = get_settings()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for caught_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(caught_signal, stop_event.set)
    supervisor = ProcessSupervisor()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        ) as http:
            runtime = _build_runtime(settings, http=http, supervisor=supervisor)
            worker_task = asyncio.create_task(runtime.worker.run_forever())
            scheduler_task = asyncio.create_task(_scheduler_loop(stop_event))
            signal_task = asyncio.create_task(stop_event.wait())
            tasks = {worker_task, scheduler_task, signal_task}
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                stop_event.set()
                await runtime.worker.stop()
                for task in done:
                    if task is not signal_task:
                        await task
            finally:
                await runtime.worker.stop()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await supervisor.terminate_all()
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
