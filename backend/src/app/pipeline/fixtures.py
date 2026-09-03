from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.pipeline.bizinfo import parse_bizinfo_page
from app.pipeline.hashing import announcement_content_hash, sha256_bytes


class StrictFixtureModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FixtureFile(StrictFixtureModel):
    path: str
    sha256: str
    source_file_id: str
    expected_extraction: Literal["NATIVE", "OCR", "LIMIT_EXCEEDED"]
    declared_size_bytes: int | None = None


class FixtureCoverageMatrix(StrictFixtureModel):
    native_hwpx: str
    native_pdf: str
    mixed_pdf: str
    vision_ocr: str
    limit_exceeded: str


class DemoFixtureManifest(StrictFixtureModel):
    announcement_id: str
    wrapper_path: str
    wrapper_sha256: str
    announcement_version_hash: str
    body_source: FixtureFile
    attachments: list[FixtureFile]
    expected_canonical_ir_path: str
    expected_ai_stages: list[str]
    coverage_matrix: FixtureCoverageMatrix


class FixtureIntegrityError(ValueError):
    pass


def _resolve_inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    root = root.resolve()
    if not path.is_relative_to(root):
        raise FixtureIntegrityError(f"Fixture path escapes manifest directory: {relative}")
    return path


def load_fixture_manifest(path: Path) -> DemoFixtureManifest:
    manifest = DemoFixtureManifest.model_validate_json(path.read_text(encoding="utf-8"))
    root = path.parent
    wrapper = _resolve_inside(root, manifest.wrapper_path)
    if sha256_bytes(wrapper.read_bytes()) != manifest.wrapper_sha256:
        raise FixtureIntegrityError("Fixture wrapper hash mismatch")
    _resolve_inside(root, manifest.expected_canonical_ir_path).read_bytes()
    body_source = _resolve_inside(root, manifest.body_source.path)
    if sha256_bytes(body_source.read_bytes()) != manifest.body_source.sha256:
        raise FixtureIntegrityError("Fixture body source hash mismatch")
    attachment_hashes: list[str] = []
    for attachment in manifest.attachments:
        candidate = _resolve_inside(root, attachment.path)
        if sha256_bytes(candidate.read_bytes()) != attachment.sha256:
            raise FixtureIntegrityError(f"Fixture attachment hash mismatch: {attachment.path}")
        if (
            attachment.expected_extraction == "LIMIT_EXCEEDED"
            and (attachment.declared_size_bytes or 0) <= 20 * 1024 * 1024
        ):
            raise FixtureIntegrityError(
                "Limit-exceeded fixture must declare a size above the 20MB file limit"
            )
        attachment_hashes.append(attachment.sha256)
    wrapper_value = load_wrapper(path, manifest)
    page = parse_bizinfo_page(wrapper_value, page_index=1, page_unit=100)
    announcement = next(
        (item for item in page.items if item.source_id == manifest.announcement_id), None
    )
    if announcement is None:
        raise FixtureIntegrityError("Manifest announcement is missing from fixture wrapper")
    calculated_version = announcement_content_hash(
        announcement.raw_item, announcement.body_text, attachment_hashes
    )
    if calculated_version != manifest.announcement_version_hash:
        raise FixtureIntegrityError("Fixture announcement version hash mismatch")
    return manifest


def load_wrapper(manifest_path: Path, manifest: DemoFixtureManifest) -> dict:
    wrapper = _resolve_inside(manifest_path.parent, manifest.wrapper_path)
    value = json.loads(wrapper.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FixtureIntegrityError("Fixture wrapper must be an object")
    return value
