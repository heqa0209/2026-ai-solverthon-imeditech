from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
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
    job, _ = await insert_job_idempotently(
        db,
        job_type=job_type,
        payload=payload,
        key=key,
    )
    return job


async def insert_job_idempotently(
    db: AsyncSession,
    *,
    job_type: str,
    payload: dict[str, Any],
    key: str,
    max_attempts: int = 3,
    available_at: datetime | None = None,
) -> tuple[Job, bool]:
    values = {
        "job_type": job_type,
        "status": "QUEUED",
        "payload": payload,
        "idempotency_key": key,
        "max_attempts": max_attempts,
        "available_at": available_at or datetime.now(UTC),
    }
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "postgresql":
        statement = (
            postgresql_insert(Job)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[Job.idempotency_key])
            .returning(Job.id)
        )
    elif dialect_name == "sqlite":
        statement = (
            sqlite_insert(Job)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[Job.idempotency_key])
            .returning(Job.id)
        )
    else:
        raise RuntimeError(f"unsupported job queue database: {dialect_name}")

    inserted_id = (await db.execute(statement)).scalar_one_or_none()
    created = inserted_id is not None
    lookup = Job.id == inserted_id if created else Job.idempotency_key == key
    job = await db.scalar(select(Job).where(lookup))
    if job is None:
        raise RuntimeError("idempotent job insert did not produce a readable row")
    return job, created
