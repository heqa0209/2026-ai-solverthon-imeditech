from __future__ import annotations

import asyncio
import sys

import pytest

from app.pipeline.processes import ProcessSupervisor


@pytest.mark.asyncio
async def test_supervisor_terminates_the_whole_child_process_group() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(10)",
        start_new_session=True,
    )
    supervisor = ProcessSupervisor(terminate_grace_seconds=0.5)
    supervisor.register(process)
    await supervisor.terminate_all()
    assert process.returncode is not None
