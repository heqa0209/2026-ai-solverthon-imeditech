from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.jobs import enqueue_job
from app.models import (
    AIStageRun,
    AnalysisRun,
    Announcement,
    AnnouncementVersion,
    CollectionSnapshot,
    SourceFile,
    new_id,
)
from app.pipeline.ai import (
    AI_STAGE_POLICIES,
    AIExecutionError,
    AIExecutor,
    AIStage,
    build_codex_invocation,
)
from app.pipeline.attachments import MAX_ANNOUNCEMENT_BYTES, AttachmentRejected
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


@dataclass(frozen=True)
class PreparedAttachmentSelection:
    input_hash: str
    output: dict[str, Any]
    duration_ms: int
    prompt_version: str
    schema_version: str


CONTRACT_ROOT = Path(__file__).with_name("contracts")


class ProductionBizinfoCollector:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        client: BizinfoClient,
        http: httpx.AsyncClient,
        source_storage_root: Path,
        executor: AIExecutor | None = None,
        contract_root: Path = CONTRACT_ROOT,
        page_unit: int = 100,
    ):
        self._sessions = sessions
        self._client = client
        self._http = http
        self._storage_root = source_storage_root.resolve()
        self._executor = executor
        self._contract_root = contract_root.resolve()
        self._page_unit = page_unit

    @staticmethod
    def _selection_required(item: ParsedAnnouncement) -> bool:
        declared_total = sum(attachment.size_bytes or 0 for attachment in item.attachments)
        return bool(item.attachments) and (
            declared_total > MAX_ANNOUNCEMENT_BYTES
            or any(attachment.size_bytes is None for attachment in item.attachments)
        )

    async def _select_attachments(
        self, item: ParsedAnnouncement, temp_root: Path
    ) -> tuple[tuple[Any, ...], PreparedAttachmentSelection | None]:
        if not self._selection_required(item):
            return item.attachments, None
        if self._executor is None:
            raise AIExecutionError(
                "ATTACHMENT_SELECTION_EXECUTOR_UNAVAILABLE",
                "Attachment selection requires the configured model executor",
                retryable=True,
            )

        instruction_path = self._contract_root / "attachment-selection-v1.prompt.txt"
        schema_path = self._contract_root / "attachment-selection-v1.schema.json"
        local_schema = temp_root / schema_path.name
        await asyncio.to_thread(shutil.copy2, schema_path, local_schema)
        output_path = temp_root / "attachment-selection-output.json"
        instruction = await asyncio.to_thread(instruction_path.read_text, encoding="utf-8")
        invocation = build_codex_invocation(
            stage=AIStage.ATTACHMENT_SELECTION,
            temp_dir=temp_root,
            schema_path=local_schema,
            output_path=output_path,
            instruction=instruction,
            structured_input={
                "announcement_id": item.source_id,
                "title": item.title,
                "summary": item.summary_text,
                "body": (item.body_text or "")[:180_000],
                "attachments": [
                    {
                        "source_order": attachment.source_order,
                        "name": attachment.name,
                        "declared_size_bytes": attachment.size_bytes,
                    }
                    for attachment in item.attachments
                ],
            },
        )
        started = time.monotonic()
        output = await self._executor.execute(invocation)
        duration_ms = max(0, int((time.monotonic() - started) * 1000))

        ranked = output.get("ranked_attachments")
        expected_orders = {attachment.source_order for attachment in item.attachments}
        if not isinstance(ranked, list):
            raise AIExecutionError(
                "ATTACHMENT_SELECTION_OUTPUT_INVALID",
                "Attachment selection did not return a ranking",
                retryable=False,
            )
        returned_orders: list[int] = []
        reasons: dict[int, str] = {}
        prioritized: dict[int, bool] = {}
        for result in ranked:
            if not isinstance(result, dict):
                break
            source_order = result.get("source_order")
            is_prioritized = result.get("prioritized")
            reason = result.get("reason")
            if (
                not isinstance(source_order, int)
                or not isinstance(is_prioritized, bool)
                or not isinstance(reason, str)
                or not reason.strip()
            ):
                break
            returned_orders.append(source_order)
            reasons[source_order] = reason.strip()
            prioritized[source_order] = is_prioritized
        if (
            len(returned_orders) != len(expected_orders)
            or len(set(returned_orders)) != len(returned_orders)
            or set(returned_orders) != expected_orders
        ):
            raise AIExecutionError(
                "ATTACHMENT_SELECTION_OUTPUT_INVALID",
                "Attachment selection must rank every attachment exactly once",
                retryable=False,
            )
        by_order = {attachment.source_order: attachment for attachment in item.attachments}
        priority_orders = [
            source_order for source_order in returned_orders if prioritized[source_order]
        ]
        remaining_orders = sorted(expected_orders - set(priority_orders))
        execution_orders = (*priority_orders, *remaining_orders)
        normalized_output = {
            "ranked_attachments": [
                {
                    "source_order": source_order,
                    "prioritized": prioritized[source_order],
                    "reason": reasons[source_order],
                }
                for source_order in execution_orders
            ]
        }
        return (
            tuple(by_order[source_order] for source_order in execution_orders),
            PreparedAttachmentSelection(
                input_hash=invocation.input_hash,
                output=normalized_output,
                duration_ms=duration_ms,
                prompt_version=instruction_path.stem,
                schema_version=schema_path.stem,
            ),
        )

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
    ) -> tuple[list[PreparedAttachment], int, PreparedAttachmentSelection | None]:
        prepared: list[PreparedAttachment] = []
        used = 0
        ordered, selection = await self._select_attachments(item, staging)
        for metadata in ordered:
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
        prepared.sort(key=lambda attachment: attachment.source_order)
        if selection is not None:
            reasons = {
                item["source_order"]: item["reason"]
                for item in selection.output["ranked_attachments"]
            }
            selection = PreparedAttachmentSelection(
                input_hash=selection.input_hash,
                output={
                    **selection.output,
                    "selection_outcomes": [
                        {
                            "source_order": attachment.source_order - 1,
                            "selected": attachment.download is not None,
                            "reason": reasons[attachment.source_order - 1],
                            "failure_code": attachment.failure_code,
                        }
                        for attachment in prepared
                    ],
                },
                duration_ms=selection.duration_ms,
                prompt_version=selection.prompt_version,
                schema_version=selection.schema_version,
            )
        return prepared, used, selection

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
            attachments, _, attachment_selection = await self._download_sources(
                item, Path(directory)
            )
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

            if attachment_selection is not None:
                selection_analysis = AnalysisRun(
                    announcement_version_id=version.id,
                    status="PENDING",
                    analysis_version="attachment-selection-v1",
                    canonical_ir=None,
                    started_at=datetime.now(UTC),
                )
                db.add(selection_analysis)
                await db.flush()
                policy = AI_STAGE_POLICIES[AIStage.ATTACHMENT_SELECTION]
                db.add(
                    AIStageRun(
                        analysis_run_id=selection_analysis.id,
                        stage=AIStage.ATTACHMENT_SELECTION.value,
                        model=policy.model,
                        effort=policy.effort,
                        prompt_version=attachment_selection.prompt_version,
                        schema_version=attachment_selection.schema_version,
                        input_hash=attachment_selection.input_hash,
                        structured_output=attachment_selection.output,
                        evidence=[],
                        duration_ms=attachment_selection.duration_ms,
                        attempt=1,
                    )
                )

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
