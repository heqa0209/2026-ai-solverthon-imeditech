from __future__ import annotations

import asyncio
import os
import signal
import socket
import tempfile
from pathlib import Path

from app.config import REPO_ROOT, get_settings
from app.db import SessionFactory, engine
from app.pipeline.handlers import build_handler_registry
from app.pipeline.isolation import FilesystemIsolationPolicy, run_filesystem_isolation_self_test
from app.pipeline.jobs import JobQueue
from app.pipeline.worker import Worker


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _isolation_policy(source_storage_root: Path) -> FilesystemIsolationPolicy:
    temp_root = Path(tempfile.gettempdir()) / "solverthon-ai-worker"
    return FilesystemIsolationPolicy(
        temp_root=temp_root,
        forbidden_read_paths=(REPO_ROOT / ".env", REPO_ROOT / "docs", source_storage_root),
        forbidden_write_path=REPO_ROOT / ".worker-isolation-probe",
    )


async def run() -> None:
    settings = get_settings()
    policy = _isolation_policy(settings.source_storage_root)

    async def isolation_check() -> bool:
        return await asyncio.to_thread(run_filesystem_isolation_self_test, policy)

    worker = Worker(
        worker_id=_worker_id(),
        queue=JobQueue(SessionFactory),
        handlers=build_handler_registry(
            sessions=SessionFactory,
            fixture_root=settings.demo_fixture_root,
        ),
        concurrency=settings.ai_max_concurrency,
        heartbeat_seconds=30,
        isolation_check=isolation_check,
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for caught_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(caught_signal, stop_event.set)

    worker_task = asyncio.create_task(worker.run_forever())
    signal_task = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait(
            {worker_task, signal_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if signal_task in done:
            await worker.stop()
        await worker_task
    finally:
        signal_task.cancel()
        await worker.stop()
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
