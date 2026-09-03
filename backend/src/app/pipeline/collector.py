from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.jobs import enqueue_job
from app.models import (
    Announcement,
    AnnouncementVersion,
    CollectionSnapshot,
    SourceFile,
    new_id,
)
from app.pipeline.ai import AIExecutionError
from app.pipeline.attachments import AttachmentRejected
from app.pipeline.bizinfo import BizinfoClient, BizinfoPage, ParsedAnnouncement, parse_bizinfo_page
from app.pipeline.collection import DailyStopDetector, unavailable_after_two_full_snapshots
from app.pipeline.downloader import AttachmentDownloadError, DownloadedFile, download_attachment
from app.pipeline.hashing import announcement_content_hash, sha256_bytes, sha256_json
from app.pipeline.jobs import Publisher


@dataclass(frozen=True)
class PreparedCollection:
    pages: tuple[BizinfoPage, ...]
    scope: str


@dataclass(frozen=True)
class PreparedAttachment:
    source_file_id: str
    name: str
    source_url: str
    source_order: int
    declared_size: int | None
    download: DownloadedFile | None
    download_status: str
    failure_code: str | None
    version_hash: str


class ProductionBizinfoCollector:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        client: BizinfoClient,
        http: httpx.AsyncClient,
        source_storage_root: Path,
        page_unit: int = 100,
    ):
        self._sessions = sessions
        self._client = client
        self._http = http
        self._storage_root = source_storage_root.resolve()
        self._page_unit = page_unit

    async def _daily_detector(self) -> DailyStopDetector | None:
        async with self._sessions() as db:
            snapshot = await db.scalar(
                select(CollectionSnapshot)
                .where(CollectionSnapshot.scope == "DAILY", CollectionSnapshot.complete.is_(True))
                .order_by(desc(CollectionSnapshot.succeeded_at))
                .limit(1)
            )
            if snapshot is None:
                return None
            rows = (
                await db.execute(
                    select(Announcement, AnnouncementVersion).join(
                        AnnouncementVersion,
                        AnnouncementVersion.id == Announcement.current_version_id,
                    )
                )
            ).all()
            known_hashes: dict[str, str] = {}
            for announcement, version in rows:
                try:
                    page = parse_bizinfo_page(version.raw_payload, page_index=1, page_unit=1)
                    item = next(
                        item for item in page.items if item.source_id == announcement.source_id
                    )
                except ValueError, StopIteration:
                    continue
                known_hashes[announcement.source_id] = item.raw_hash
            succeeded_at = snapshot.succeeded_at
            if succeeded_at.tzinfo is None:
                succeeded_at = succeeded_at.replace(tzinfo=UTC)
            return DailyStopDetector(succeeded_at, known_hashes)

    async def _fetch_pages(self, scope: str) -> tuple[BizinfoPage, ...]:
        detector = await self._daily_detector() if scope == "DAILY" else None
        pages: list[BizinfoPage] = []
        seen = 0
        for page_index in range(1, 10_001):
            try:
                page = await self._client.fetch_page(
                    page_index=page_index, page_unit=self._page_unit
                )
            except (httpx.HTTPError, ValueError) as exc:
                raise AIExecutionError(
                    "BIZINFO_FETCH_FAILED", "Bizinfo collection did not complete", retryable=True
                ) from exc
            pages.append(page)
            seen += len(page.items)
            if not page.items:
                break
            if detector is not None and detector.observe(page):
                break
            if page.total_count is not None and seen >= page.total_count:
                break
        else:
            raise AIExecutionError(
                "BIZINFO_PAGE_LIMIT_EXCEEDED",
                "Bizinfo pagination exceeded safety limit",
                retryable=False,
            )
        return tuple(pages)

    async def prepare(self, *, scope: str, context: Any) -> Publisher:
        if scope not in {"DAILY", "FULL"}:
            raise AIExecutionError(
                "COLLECTION_SCOPE_INVALID",
                "Collection scope must be DAILY or FULL",
                retryable=False,
            )
        pages = await self._fetch_pages(scope)

        async def publish(db: AsyncSession, _job: Any) -> None:
            await self._persist(db, PreparedCollection(pages, scope))

        return publish

    async def _download_sources(
        self, item: ParsedAnnouncement, staging: Path
    ) -> tuple[list[PreparedAttachment], int]:
        prepared: list[PreparedAttachment] = []
        used = 0
        for metadata in item.attachments:
            source_id = new_id()
            safe_name = Path(metadata.name).name or f"attachment-{metadata.source_order}"
            target = staging / f"{metadata.source_order:03d}-{source_id}-{safe_name}"
            download = None
            status = "PENDING"
            failure = None
            try:
                download = await download_attachment(
                    self._http,
                    url=metadata.url,
                    filename=safe_name,
                    target=target,
                    declared_size=metadata.size_bytes,
                    announcement_bytes=used,
                )
                used += download.size_bytes
                status = "SUCCEEDED"
                version_hash = download.sha256
            except AttachmentRejected as exc:
                status = "LIMIT_EXCEEDED" if "LIMIT_EXCEEDED" in exc.code else "FAILED_FINAL"
                failure = exc.code
                version_hash = sha256_json(
                    {"url": metadata.url, "name": safe_name, "failure": failure}
                )
            except AttachmentDownloadError as exc:
                status = "FAILED_FINAL"
                failure = exc.code
                version_hash = sha256_json(
                    {"url": metadata.url, "name": safe_name, "failure": failure}
                )
            prepared.append(
                PreparedAttachment(
                    source_file_id=source_id,
                    name=safe_name,
                    source_url=metadata.url,
                    source_order=metadata.source_order + 1,
                    declared_size=metadata.size_bytes,
                    download=download,
                    download_status=status,
                    failure_code=failure,
                    version_hash=version_hash,
                )
            )
        return prepared, used

    def _store_body(self, version_id: str, body_text: str) -> tuple[str, str, int]:
        data = body_text.encode("utf-8")
        relative = Path(version_id) / "000-bizinfo-body.txt"
        target = (self._storage_root / relative).resolve()
        if not target.is_relative_to(self._storage_root):
            raise ValueError("Source storage path escaped root")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.chmod(target, 0o600)
        return relative.as_posix(), sha256_bytes(data), len(data)

    async def _persist_item(
        self, db: AsyncSession, page: BizinfoPage, item: ParsedAnnouncement
    ) -> bool:
        self._storage_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            dir=self._storage_root, prefix=".collection-"
        ) as directory:
            attachments, _ = await self._download_sources(item, Path(directory))
            content_hash = announcement_content_hash(
                item.raw_item,
                item.body_text,
                [attachment.version_hash for attachment in attachments],
            )
            announcement = await db.scalar(
                select(Announcement).where(Announcement.source_id == item.source_id)
            )
            if announcement is None:
                announcement = Announcement(
                    source_id=item.source_id,
                    source_url=item.source_url,
                    source_available=True,
                )
                db.add(announcement)
                await db.flush()
            else:
                announcement.source_url = item.source_url
                announcement.source_available = True
            existing = await db.scalar(
                select(AnnouncementVersion).where(
                    AnnouncementVersion.announcement_id == announcement.id,
                    AnnouncementVersion.content_hash == content_hash,
                )
            )
            if existing is not None:
                announcement.current_version_id = existing.id
                return False

            version = AnnouncementVersion(
                announcement_id=announcement.id,
                raw_payload=page.raw_payload,
                content_hash=content_hash,
                title=item.title,
                agency_name=item.agency_name,
                summary_text=item.summary_text,
                body_text=item.body_text,
                published_on=item.published_on,
                recruitment_starts_on=item.recruitment_starts_on,
                recruitment_ends_on=item.recruitment_ends_on,
            )
            db.add(version)
            await db.flush()
            announcement.current_version_id = version.id

            body_text = item.body_text or ""
            body_path, body_hash, body_size = self._store_body(version.id, body_text)
            db.add(
                SourceFile(
                    announcement_version_id=version.id,
                    name="bizinfo-body.txt",
                    source_url=item.source_url,
                    storage_path=body_path,
                    sha256=body_hash,
                    mime_type="text/plain",
                    size_bytes=body_size,
                    source_order=0,
                    source_priority=10,
                    download_status="SUCCEEDED",
                    extraction_status="SUCCEEDED",
                    extracted_text=body_text,
                )
            )
            for attachment in attachments:
                row = SourceFile(
                    id=attachment.source_file_id,
                    announcement_version_id=version.id,
                    name=attachment.name,
                    source_url=attachment.source_url,
                    storage_path=None,
                    sha256=None,
                    mime_type=None,
                    size_bytes=attachment.declared_size,
                    source_order=attachment.source_order,
                    source_priority=20,
                    download_status="PENDING",
                    extraction_status="PENDING",
                )
                db.add(row)
                await db.flush()
                if attachment.download is not None:
                    relative = Path(version.id) / attachment.download.path.name
                    final_path = (self._storage_root / relative).resolve()
                    if not final_path.is_relative_to(self._storage_root):
                        raise ValueError("Source storage path escaped root")
                    final_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(attachment.download.path, final_path)
                    os.chmod(final_path, 0o600)
                    row.storage_path = relative.as_posix()
                    row.sha256 = attachment.download.sha256
                    row.mime_type = attachment.download.mime_type
                    row.size_bytes = attachment.download.size_bytes
                    row.download_status = "SUCCEEDED"
                else:
                    row.download_status = attachment.download_status
                    row.extraction_status = "SKIPPED"
                    row.failure_code = attachment.failure_code
            await enqueue_job(
                db,
                "ANNOUNCEMENT_ANALYZE",
                {"announcementId": announcement.id, "announcementVersionId": version.id},
            )
            return True

    async def _persist(self, db: AsyncSession, collection: PreparedCollection) -> None:
        unique: dict[str, tuple[BizinfoPage, ParsedAnnouncement]] = {}
        for page in collection.pages:
            for item in page.items:
                unique.setdefault(item.source_id, (page, item))
        for page, item in unique.values():
            await self._persist_item(db, page, item)

        source_ids = sorted(unique)
        if collection.scope == "FULL":
            previous = await db.scalar(
                select(CollectionSnapshot)
                .where(CollectionSnapshot.scope == "FULL", CollectionSnapshot.complete.is_(True))
                .order_by(desc(CollectionSnapshot.succeeded_at))
                .limit(1)
            )
            if previous is not None:
                known = set((await db.scalars(select(Announcement.source_id))).all())
                unavailable = unavailable_after_two_full_snapshots(
                    known_source_ids=known,
                    previous_snapshot=set(previous.source_ids),
                    current_snapshot=set(source_ids),
                )
                if unavailable:
                    rows = list(
                        (
                            await db.scalars(
                                select(Announcement).where(Announcement.source_id.in_(unavailable))
                            )
                        ).all()
                    )
                    for row in rows:
                        row.source_available = False
        db.add(
            CollectionSnapshot(
                scope=collection.scope,
                source_ids=source_ids,
                complete=True,
                succeeded_at=datetime.now(UTC),
            )
        )
