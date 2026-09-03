from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.pipeline.attachments import (
    MAX_FILE_BYTES,
    AttachmentRejected,
    detected_format,
    enforce_download_budget,
    safe_extract_zip,
)


class AttachmentDownloadError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DownloadedFile:
    path: Path
    size_bytes: int
    sha256: str
    mime_type: str
    detected_format: str


_FORMAT_MIME = {
    "PDF": "application/pdf",
    "HWP": "application/x-hwp",
    "HWPX": "application/x-hwpx",
    "DOCX": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "XLSX": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ZIP": "application/zip",
    "IMAGE": "image/*",
}


def _validate_content_type(content_type: str | None, detected: str) -> str:
    expected = _FORMAT_MIME.get(detected, "application/octet-stream")
    actual = (content_type or "").split(";", 1)[0].strip().casefold()
    if actual and actual != "application/octet-stream":
        matches = actual.startswith("image/") if expected == "image/*" else actual == expected
        if not matches:
            raise AttachmentRejected(
                "MIME_SIGNATURE_MISMATCH", f"Content-Type {actual} does not match {detected}"
            )
    return actual or ("application/octet-stream" if expected == "image/*" else expected)


async def download_attachment(
    client: httpx.AsyncClient,
    *,
    url: str,
    filename: str,
    target: Path,
    declared_size: int | None,
    announcement_bytes: int,
    max_attempts: int = 3,
) -> DownloadedFile:
    """Stream one bounded attachment and validate bytes before publishing it."""

    if declared_size is not None:
        enforce_download_budget(file_size=declared_size, announcement_bytes=announcement_bytes)
    target.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    content_type: str | None = None
    for attempt in range(1, max_attempts + 1):
        await asyncio.to_thread(target.unlink, missing_ok=True)
        digest = hashlib.sha256()
        written = 0
        try:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type")
                with target.open("xb") as output:
                    os.chmod(target, 0o600)
                    async for chunk in response.aiter_bytes(64 * 1024):
                        written += len(chunk)
                        if written > MAX_FILE_BYTES:
                            raise AttachmentRejected(
                                "FILE_LIMIT_EXCEEDED", "Attachment exceeds 20MB"
                            )
                        enforce_download_budget(
                            file_size=written, announcement_bytes=announcement_bytes
                        )
                        output.write(chunk)
                        digest.update(chunk)
            break
        except AttachmentRejected:
            await asyncio.to_thread(target.unlink, missing_ok=True)
            raise
        except (httpx.HTTPError, OSError) as exc:
            await asyncio.to_thread(target.unlink, missing_ok=True)
            last_error = exc
            if attempt == max_attempts:
                raise AttachmentDownloadError(
                    "ATTACHMENT_DOWNLOAD_FAILED", "Attachment download failed after 3 attempts"
                ) from exc
    else:  # pragma: no cover - the loop always returns or raises
        raise AttachmentDownloadError("ATTACHMENT_DOWNLOAD_FAILED", str(last_error))

    data = await asyncio.to_thread(target.read_bytes)
    detected = detected_format(filename, data)
    mime_type = _validate_content_type(content_type, detected)
    if detected == "ZIP":
        with tempfile.TemporaryDirectory(prefix="solverthon-zip-check-") as directory:
            safe_extract_zip(target, Path(directory))
    return DownloadedFile(target, written, digest.hexdigest(), mime_type, detected)
