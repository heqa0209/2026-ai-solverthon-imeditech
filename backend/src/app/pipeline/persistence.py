from __future__ import annotations

import asyncio
import mimetypes
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.decision_service import publish_deterministic_decision
from app.models import (
    AnalysisRun,
    Announcement,
    AnnouncementVersion,
    CompanyProfile,
    EligibilityDecision,
    ExtractedCondition,
    SourceFile,
)
from app.pipeline.attachments import store_attachment
from app.pipeline.bizinfo import parse_bizinfo_page
from app.pipeline.extraction import extract_native
from app.pipeline.fixtures import (
    DemoFixtureManifest,
    FixtureFile,
    load_fixture_manifest,
    load_wrapper,
)
from app.pipeline.hashing import sha256_bytes
from app.pipeline.ir import CanonicalIR, EvidenceSource, validate_evidence


@dataclass(frozen=True)
class FixtureLoadResult:
    processed: int
    announcement_id: str
    announcement_version_id: str
    analysis_run_id: str
    decisions_published: int
    version_created: bool
    analysis_created: bool


def _prepare_storage_root(path: Path) -> Path:
    root = path.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _fixture_path(manifest_path: Path, relative: str) -> Path:
    root = manifest_path.parent.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Fixture path escapes manifest directory")
    return path


def _source_text(manifest_path: Path, source: FixtureFile) -> tuple[str, str | None]:
    path = _fixture_path(manifest_path, source.path)
    if source.expected_extraction == "LIMIT_EXCEEDED":
        return "", "FIXTURE_LIMIT_EXCEEDED"
    if path.suffix.casefold() == ".txt":
        return path.read_text(encoding="utf-8"), None
    extraction = extract_native(path)
    if source.expected_extraction == "OCR" and extraction.text:
        raise ValueError("OCR fixture unexpectedly contains native text")
    return extraction.text, None if extraction.text else "FIXTURE_OCR_REQUIRED"


def _store_fixture_source(
    source_path: Path,
    *,
    source: FixtureFile,
    version: AnnouncementVersion,
    source_storage_root: Path,
) -> str | None:
    if source.expected_extraction == "LIMIT_EXCEEDED":
        return None
    relative = Path(version.id) / f"{source.source_file_id}-{source_path.name}"
    target = (source_storage_root / relative).resolve()
    if not target.is_relative_to(source_storage_root):
        raise ValueError("Fixture storage path escapes source root")
    if target.is_file():
        if sha256_bytes(target.read_bytes()) != source.sha256:
            raise ValueError(f"Stored fixture hash mismatch: {source.path}")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        stored = store_attachment(source_path, target)
        if stored.sha256 != source.sha256:
            raise ValueError(f"Stored fixture hash mismatch: {source.path}")
    return relative.as_posix()


async def _upsert_source_file(
    db: AsyncSession,
    *,
    manifest_path: Path,
    source: FixtureFile,
    version: AnnouncementVersion,
    source_url: str,
    order: int,
    priority: int,
    source_storage_root: Path,
) -> EvidenceSource:
    text, failure_code = _source_text(manifest_path, source)
    path = _fixture_path(manifest_path, source.path)
    row = await db.get(SourceFile, source.source_file_id)
    if row is not None and row.announcement_version_id != version.id:
        raise ValueError(f"Fixture source id already belongs to another version: {row.id}")
    storage_path = _store_fixture_source(
        path,
        source=source,
        version=version,
        source_storage_root=source_storage_root,
    )
    if row is None:
        row = SourceFile(
            id=source.source_file_id,
            announcement_version_id=version.id,
            name=path.name,
            source_url=source_url,
            storage_path=storage_path,
            sha256=source.sha256,
            mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            size_bytes=path.stat().st_size,
            source_order=order,
            source_priority=priority,
            download_status=(
                "LIMIT_EXCEEDED" if source.expected_extraction == "LIMIT_EXCEEDED" else "SUCCEEDED"
            ),
            extraction_status="SUCCEEDED" if text else "SKIPPED",
            failure_code=failure_code,
            extracted_text=text or None,
        )
        db.add(row)
        await db.flush()
    else:
        if row.sha256 != source.sha256 or (row.extracted_text or "") != text:
            raise ValueError(f"Immutable fixture source does not match manifest: {row.id}")
        row.storage_path = storage_path
        row.download_status = (
            "LIMIT_EXCEEDED" if source.expected_extraction == "LIMIT_EXCEEDED" else "SUCCEEDED"
        )
    return EvidenceSource(
        source_file_id=row.id,
        source_version=source.sha256,
        text=text,
    )


async def persist_demo_fixture(
    db: AsyncSession,
    manifest_path: Path,
    *,
    source_storage_root: Path,
    publish_for_profiles: bool = True,
) -> FixtureLoadResult:
    """Persist one validated fixture and publish deterministic decisions atomically."""

    manifest: DemoFixtureManifest = load_fixture_manifest(manifest_path)
    source_storage_root = await asyncio.to_thread(_prepare_storage_root, source_storage_root)
    wrapper = load_wrapper(manifest_path, manifest)
    page = parse_bizinfo_page(wrapper, page_index=1, page_unit=100)
    parsed = next(item for item in page.items if item.source_id == manifest.announcement_id)
    body_text, body_failure = _source_text(manifest_path, manifest.body_source)
    if body_failure is not None or body_text.strip() != (parsed.body_text or "").strip():
        raise ValueError("Fixture body source must exactly match the preserved wrapper body")
    canonical_ir = CanonicalIR.model_validate_json(
        _fixture_path(manifest_path, manifest.expected_canonical_ir_path).read_text(
            encoding="utf-8"
        )
    )

    announcement = await db.scalar(
        select(Announcement).where(Announcement.source_id == parsed.source_id)
    )
    if announcement is None:
        announcement = Announcement(
            source_id=parsed.source_id,
            source_url=parsed.source_url,
            source_available=True,
        )
        db.add(announcement)
        await db.flush()
    else:
        announcement.source_url = parsed.source_url
        announcement.source_available = True

    version = await db.scalar(
        select(AnnouncementVersion).where(
            AnnouncementVersion.announcement_id == announcement.id,
            AnnouncementVersion.content_hash == manifest.announcement_version_hash,
        )
    )
    version_created = version is None
    if version is None:
        version = AnnouncementVersion(
            announcement_id=announcement.id,
            raw_payload=wrapper,
            content_hash=manifest.announcement_version_hash,
            title=parsed.title,
            agency_name=parsed.agency_name,
            summary_text=parsed.summary_text,
            body_text=parsed.body_text,
            published_on=parsed.published_on,
            recruitment_starts_on=parsed.recruitment_starts_on,
            recruitment_ends_on=parsed.recruitment_ends_on,
        )
        db.add(version)
        await db.flush()
    announcement.current_version_id = version.id

    evidence_sources = [
        await _upsert_source_file(
            db,
            manifest_path=manifest_path,
            source=manifest.body_source,
            version=version,
            source_url=parsed.source_url,
            order=0,
            priority=10,
            source_storage_root=source_storage_root,
        )
    ]
    for order, source in enumerate(manifest.attachments, start=1):
        metadata = next(
            (item for item in parsed.attachments if item.source_order == order - 1), None
        )
        evidence_sources.append(
            await _upsert_source_file(
                db,
                manifest_path=manifest_path,
                source=source,
                version=version,
                source_url=metadata.url if metadata else parsed.source_url,
                order=order,
                priority=20,
                source_storage_root=source_storage_root,
            )
        )
    validate_evidence(canonical_ir, evidence_sources)

    analysis = await db.scalar(
        select(AnalysisRun).where(
            AnalysisRun.announcement_version_id == version.id,
            AnalysisRun.analysis_version == canonical_ir.analysis_version,
            AnalysisRun.status == "SUCCEEDED",
        )
    )
    analysis_created = analysis is None
    if analysis is None:
        now = datetime.now(UTC)
        analysis = AnalysisRun(
            announcement_version_id=version.id,
            status="SUCCEEDED",
            analysis_version=canonical_ir.analysis_version,
            canonical_ir=canonical_ir.model_dump(mode="json"),
            started_at=now,
            completed_at=now,
        )
        db.add(analysis)
        await db.flush()
        groups = {group.group_id: group for group in canonical_ir.groups}
        for condition in canonical_ir.conditions:
            group = groups[condition.group_id]
            db.add(
                ExtractedCondition(
                    analysis_run_id=analysis.id,
                    condition_key=condition.condition_id,
                    group_key=condition.group_id,
                    track_key=group.track_ids[0] if len(group.track_ids) == 1 else None,
                    role_key=group.role_keys[0] if len(group.role_keys) == 1 else None,
                    kind=condition.kind.value,
                    subject=condition.subject.value,
                    operator=condition.operator.value,
                    expected_value=(
                        condition.expected_value.model_dump(mode="json")
                        if condition.expected_value is not None
                        else None
                    ),
                    unit=condition.unit,
                    reference_date=condition.reference_date,
                    evidence=[item.model_dump(mode="json") for item in condition.evidence],
                )
            )
        await db.flush()

    decisions_published = 0
    if publish_for_profiles:
        profiles = list(
            (
                await db.scalars(
                    select(CompanyProfile).where(CompanyProfile.current_version_id.is_not(None))
                )
            ).all()
        )
        for profile in profiles:
            current = await db.scalar(
                select(EligibilityDecision).where(
                    EligibilityDecision.user_id == profile.user_id,
                    EligibilityDecision.announcement_id == announcement.id,
                    EligibilityDecision.announcement_version_id == version.id,
                    EligibilityDecision.company_profile_version_id == profile.current_version_id,
                    EligibilityDecision.is_current.is_(True),
                )
            )
            if current is not None:
                continue
            await publish_deterministic_decision(
                db,
                user_id=profile.user_id,
                announcement_id=announcement.id,
                announcement_version_id=version.id,
                company_profile_version_id=profile.current_version_id,
                analysis_run_id=analysis.id,
                selected_role_key=None,
            )
            decisions_published += 1

    processed = int(version_created or analysis_created or decisions_published > 0)
    return FixtureLoadResult(
        processed=processed,
        announcement_id=announcement.id,
        announcement_version_id=version.id,
        analysis_run_id=analysis.id,
        decisions_published=decisions_published,
        version_created=version_created,
        analysis_created=analysis_created,
    )
