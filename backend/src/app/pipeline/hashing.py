from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def announcement_content_hash(
    raw_payload: Mapping[str, Any],
    body_text: str | None,
    attachment_hashes: Iterable[str],
) -> str:
    """Hash every source input that can change an announcement analysis."""

    return sha256_json(
        {
            "raw_payload": raw_payload,
            "body_text": body_text,
            "attachment_hashes": sorted(attachment_hashes),
        }
    )


@dataclass(frozen=True)
class VersionCandidate:
    source_id: str
    source_url: str
    raw_payload: dict[str, Any]
    body_text: str | None
    attachment_hashes: tuple[str, ...]
    content_hash: str


def build_version_candidate(
    *,
    source_id: str,
    source_url: str,
    raw_payload: dict[str, Any],
    body_text: str | None,
    attachment_hashes: Iterable[str] = (),
) -> VersionCandidate:
    hashes = tuple(sorted(attachment_hashes))
    return VersionCandidate(
        source_id=source_id,
        source_url=source_url,
        raw_payload=raw_payload,
        body_text=body_text,
        attachment_hashes=hashes,
        content_hash=announcement_content_hash(raw_payload, body_text, hashes),
    )


class VersionStore(Protocol):
    """Backend-owned transaction hook for immutable version publication."""

    async def persist_candidate(self, candidate: VersionCandidate) -> tuple[str, bool]:
        """Return ``(announcement_version_id, created)`` atomically."""
