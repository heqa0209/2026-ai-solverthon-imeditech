from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.models import Job
from app.pipeline.ai import AIExecutionError
from app.pipeline.jobs import JobQueue, LostLeaseError, Publisher
from app.pipeline.processes import ProcessSupervisor

JobHandler = Callable[[dict[str, Any], "JobContext"], Awaitable["JobOutcome"]]
IsolationCheck = Callable[[], Awaitable[bool]]


@dataclass(frozen=True)
class JobOutcome:
    publisher: Publisher | None = None


@dataclass
class JobContext:
    job_id: str
    owner: str
    queue: JobQueue
    supervisor: ProcessSupervisor = field(default_factory=ProcessSupervisor)
    lease_lost: asyncio.Event = field(default_factory=asyncio.Event)

    async def assert_lease(self) -> None:
        if self.lease_lost.is_set() or not await self.queue.lease_is_current(
            job_id=self.job_id, owner=self.owner
        ):
            self.lease_lost.set()
            raise LostLeaseError(self.job_id)


class Worker:
    def __init__(
        self,
        *,
        worker_id: str,
        queue: JobQueue,
        handlers: Mapping[str, JobHandler],
        concurrency: int = 5,
        heartbeat_seconds: float = 30,
        poll_seconds: float = 1,
        isolation_check: IsolationCheck | None = None,
    ):
        if not 1 <= concurrency <= 5:
            raise ValueError("Worker concurrency must be between 1 and 5")
        self.worker_id = worker_id
        self.queue = queue
        self.handlers = dict(handlers)
        self.concurrency = concurrency
        self.heartbeat_seconds = heartbeat_seconds
        self.poll_seconds = poll_seconds
        self.isolation_check = isolation_check
        self._stopping = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._worker_heartbeat_task: asyncio.Task[None] | None = None
        self._isolation_ok = False

    @property
    def isolation_ok(self) -> bool:
        return self._isolation_ok

    async def ensure_ready(self) -> None:
        if self.isolation_check is None:
            await self.queue.record_worker_heartbeat(worker_id=self.worker_id, isolation_ok=False)
            raise RuntimeError("Worker isolation self-test is not configured")
        self._isolation_ok = await self.isolation_check()
        await self.queue.record_worker_heartbeat(
            worker_id=self.worker_id, isolation_ok=self._isolation_ok
        )
        if not self._isolation_ok:
            raise RuntimeError("Worker isolation self-test failed")

    async def _heartbeat(self, context: JobContext) -> None:
        while not self._stopping.is_set() and not context.lease_lost.is_set():
            await asyncio.sleep(self.heartbeat_seconds)
            if not await self.queue.heartbeat(job_id=context.job_id, owner=context.owner):
                context.lease_lost.set()
                await context.supervisor.terminate_all()
                return

    async def _worker_heartbeat(self) -> None:
        while not self._stopping.is_set():
            await self.queue.record_worker_heartbeat(
                worker_id=self.worker_id,
                isolation_ok=self._isolation_ok,
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self.heartbeat_seconds)

    async def _cleanup_worker_heartbeat(self) -> None:
        heartbeat = self._worker_heartbeat_task
        self._worker_heartbeat_task = None
        if heartbeat is not None:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        await self.queue.delete_worker_heartbeat(worker_id=self.worker_id)

    async def _execute(self, job: Job) -> None:
        context = JobContext(job.id, self.worker_id, self.queue)
        heartbeat = asyncio.create_task(self._heartbeat(context))
        handler = self.handlers.get(job.job_type)
        try:
            if handler is None:
                raise AIExecutionError("JOB_HANDLER_MISSING", job.job_type, retryable=False)
            handler_task = asyncio.create_task(handler(job.payload, context))
            lease_task = asyncio.create_task(context.lease_lost.wait())
            done, _ = await asyncio.wait(
                {handler_task, lease_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if lease_task in done and context.lease_lost.is_set():
                handler_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await handler_task
                raise LostLeaseError(job.id)
            lease_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await lease_task
            outcome = await handler_task
            await context.assert_lease()
            await self.queue.complete(
                job_id=job.id,
                owner=self.worker_id,
                publisher=outcome.publisher,
            )
        except LostLeaseError:
            await context.supervisor.terminate_all()
        except asyncio.CancelledError:
            await context.supervisor.terminate_all()
            with contextlib.suppress(LostLeaseError):
                await self.queue.fail(
                    job_id=job.id,
                    owner=self.worker_id,
                    error_code="WORKER_STOPPED",
                    error_message="Worker stopped before the job completed",
                    retryable=True,
                )
            raise
        except AIExecutionError as exc:
            with contextlib.suppress(LostLeaseError):
                await self.queue.fail(
                    job_id=job.id,
                    owner=self.worker_id,
                    error_code=exc.code,
                    error_message=str(exc),
                    retryable=exc.retryable,
                )
        except Exception as exc:
            with contextlib.suppress(LostLeaseError):
                await self.queue.fail(
                    job_id=job.id,
                    owner=self.worker_id,
                    error_code="UNHANDLED_JOB_ERROR",
                    error_message=str(exc),
                    retryable=False,
                )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await context.supervisor.terminate_all()

    async def run_once(self) -> int:
        if not self._isolation_ok:
            await self.ensure_ready()
        else:
            await self.queue.record_worker_heartbeat(worker_id=self.worker_id, isolation_ok=True)
        claimed: list[Job] = []
        while len(claimed) < self.concurrency:
            job = await self.queue.claim(owner=self.worker_id)
            if job is None:
                break
            claimed.append(job)
        for job in claimed:
            task = asyncio.create_task(self._execute(job))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        if self._tasks:
            await asyncio.gather(*list(self._tasks))
        return len(claimed)

    async def run_forever(self) -> None:
        if not self._isolation_ok:
            await self.ensure_ready()
        self._worker_heartbeat_task = asyncio.create_task(self._worker_heartbeat())
        try:
            while not self._stopping.is_set():
                started = await self.run_once()
                if started == 0:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_seconds)
        finally:
            await self._cleanup_worker_heartbeat()

    async def stop(self) -> None:
        self._stopping.set()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        await self._cleanup_worker_heartbeat()
