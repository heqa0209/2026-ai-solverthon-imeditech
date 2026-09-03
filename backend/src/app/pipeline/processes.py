from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass, field


@dataclass
class ProcessSupervisor:
    """Own child process groups so cancellation cannot leave Codex running."""

    terminate_grace_seconds: float = 5.0
    _processes: dict[int, asyncio.subprocess.Process] = field(default_factory=dict)

    def register(self, process: asyncio.subprocess.Process) -> None:
        if process.pid is None:
            raise ValueError("Cannot supervise a process without a pid")
        self._processes[process.pid] = process

    def forget(self, process: asyncio.subprocess.Process) -> None:
        if process.pid is not None:
            self._processes.pop(process.pid, None)

    async def terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            self.forget(process)
            return
        if process.pid is None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            self.forget(process)
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=self.terminate_grace_seconds)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        finally:
            self.forget(process)

    async def terminate_all(self) -> None:
        await asyncio.gather(
            *(self.terminate(process) for process in list(self._processes.values()))
        )
