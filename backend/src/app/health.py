from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_db
from app.models import WorkerHeartbeat
from app.schemas import HealthResponse, ReadinessCheck, ReadinessResponse

router = APIRouter(prefix="/api/v1", tags=["health"])
CODEX_LOGIN_TIMEOUT_SECONDS = 5.0


def _age(value: datetime) -> timedelta:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return datetime.now(UTC) - value


@router.get("/health/live", response_model=HealthResponse)
async def liveness() -> HealthResponse:
    return HealthResponse()


async def _codex_login_status(timeout_seconds: float = CODEX_LOGIN_TIMEOUT_SECONDS) -> bool:
    executable = shutil.which("codex")
    if executable is None:
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "login",
            "status",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        return await asyncio.wait_for(process.wait(), timeout=timeout_seconds) == 0
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=1)
        return False


async def get_codex_login_status() -> bool:
    return await _codex_login_status()


@router.get("/ops/health/ready", response_model=ReadinessResponse)
async def readiness(
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    codex_login_ok: Annotated[bool, Depends(get_codex_login_status)],
) -> ReadinessResponse:
    checks: dict[str, ReadinessCheck] = {}
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = ReadinessCheck(status="ok")
    except Exception:
        checks["database"] = ReadinessCheck(status="error", detail="database unavailable")

    storage = settings.source_storage_root
    storage_ok = storage.is_dir() and os.access(storage, os.R_OK | os.W_OK)
    checks["sourceStorage"] = ReadinessCheck(
        status="ok" if storage_ok else "error",
        detail=None if storage_ok else "source storage unavailable",
    )
    checks["bizinfoCredential"] = ReadinessCheck(
        status="ok" if bool(settings.bizinfo_api_key) else "error",
        detail=None if settings.bizinfo_api_key else "credential missing",
    )
    checks["codexCli"] = ReadinessCheck(
        status="ok" if codex_login_ok else "error",
        detail=None if codex_login_ok else "Codex login unavailable",
    )
    try:
        heartbeat = await db.scalar(
            select(WorkerHeartbeat).order_by(desc(WorkerHeartbeat.heartbeat_at)).limit(1)
        )
        worker_ok = bool(
            heartbeat
            and heartbeat.isolation_ok
            and _age(heartbeat.heartbeat_at) <= timedelta(seconds=90)
        )
    except Exception:
        worker_ok = False
    checks["worker"] = ReadinessCheck(
        status="ok" if worker_ok else "error",
        detail=None if worker_ok else "worker heartbeat unavailable",
    )
    status = "ok" if all(check.status == "ok" for check in checks.values()) else "error"
    return ReadinessResponse(status=status, checks=checks)
