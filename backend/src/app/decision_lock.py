from __future__ import annotations

import hashlib

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Announcement


def decision_lock_key(user_id: str, announcement_id: str) -> int:
    digest = hashlib.blake2b(f"{user_id}\0{announcement_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


async def serialize_decision_state(db: AsyncSession, *, user_id: str, announcement_id: str) -> None:
    """Serialize all writes that can change or publish one user's decision state."""
    if db.get_bind().dialect.name == "postgresql":
        lock_key = decision_lock_key(user_id, announcement_id)
        await db.execute(select(func.pg_advisory_xact_lock(lock_key)))
        return
    await db.execute(
        select(Announcement.id).where(Announcement.id == announcement_id).with_for_update()
    )
