from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job


def idempotency_key(job_type: str, payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{job_type}:{encoded}".encode()).hexdigest()


async def enqueue_job(
    db: AsyncSession,
    job_type: str,
    payload: dict[str, object],
) -> Job:
    key = idempotency_key(job_type, payload)
    existing = await db.scalar(select(Job).where(Job.idempotency_key == key))
    if existing is not None:
        return existing
    job = Job(
        job_type=job_type,
        status="QUEUED",
        payload=payload,
        idempotency_key=key,
        available_at=datetime.now(UTC),
    )
    db.add(job)
    await db.flush()
    return job
