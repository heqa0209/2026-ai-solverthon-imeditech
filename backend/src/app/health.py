from __future__ import annotations

import os
import shutil
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


def _age(value: datetime) -> timedelta:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return datetime.now(UTC) - value


@router.get("/health/live", response_model=HealthResponse)
async def liveness() -> HealthResponse:
    return HealthResponse()


@router.get("/ops/health/ready", response_model=ReadinessResponse)
async def readiness(
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
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
        status="ok" if shutil.which("codex") else "error",
        detail=None if shutil.which("codex") else "Codex CLI unavailable",
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
