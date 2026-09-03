from __future__ import annotations

import io
import os
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.pipeline.hashing import sha256_bytes

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ANNOUNCEMENT_BYTES = 100 * 1024 * 1024
MAX_ZIP_ENTRIES = 200

_EXECUTABLE_SUFFIXES = {
    ".app",
    ".bat",
    ".cmd",
    ".com",
    ".dll",
    ".dylib",
    ".exe",
    ".jar",
    ".js",
    ".msi",
    ".ps1",
    ".scr",
    ".sh",
    ".so",
}


class AttachmentRejected(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StoredAttachment:
    relative_path: str
    size_bytes: int
    sha256: str
    nested_archive: bool


def enforce_download_budget(*, file_size: int, announcement_bytes: int) -> None:
    if file_size < 0:
        raise AttachmentRejected("INVALID_SIZE", "Attachment size cannot be negative")
    if file_size > MAX_FILE_BYTES:
        raise AttachmentRejected("FILE_LIMIT_EXCEEDED", "Attachment exceeds 20MB")
    if announcement_bytes + file_size > MAX_ANNOUNCEMENT_BYTES:
        raise AttachmentRejected("ANNOUNCEMENT_LIMIT_EXCEEDED", "Announcement exceeds 100MB")


def _safe_member_path(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) >= 2 and normalized[1] == ":"):
        raise AttachmentRejected("ZIP_UNSAFE_PATH", f"Absolute ZIP path rejected: {name}")
    path = PurePosixPath(normalized)
    if not normalized or any(part in {"", ".", ".."} for part in path.parts):
        raise AttachmentRejected("ZIP_UNSAFE_PATH", f"Unsafe ZIP path rejected: {name}")
    return path


def _validate_member(info: zipfile.ZipInfo) -> PurePosixPath:
    path = _safe_member_path(info.filename)
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if stat.S_ISLNK(mode):
        raise AttachmentRejected("ZIP_SYMLINK", f"ZIP symlink rejected: {info.filename}")
    if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise AttachmentRejected("ZIP_SPECIAL_FILE", f"ZIP special file rejected: {info.filename}")
    if path.suffix.casefold() in _EXECUTABLE_SUFFIXES:
        raise AttachmentRejected(
            "ZIP_EXECUTABLE", f"Executable ZIP member rejected: {info.filename}"
        )
    if info.file_size > MAX_FILE_BYTES:
        raise AttachmentRejected("FILE_LIMIT_EXCEEDED", f"ZIP member exceeds 20MB: {info.filename}")
    return path


def safe_extract_zip(
    archive: Path,
    destination: Path,
    *,
    announcement_bytes: int = 0,
) -> tuple[StoredAttachment, ...]:
    """Extract one ZIP layer after validating every member and actual byte count."""

    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    extracted: list[StoredAttachment] = []
    created_paths: list[Path] = []
    running_total = announcement_bytes
    try:
        with zipfile.ZipFile(archive) as bundle:
            infos = bundle.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise AttachmentRejected("ZIP_ENTRY_LIMIT_EXCEEDED", "ZIP exceeds 200 entries")
            validated = [(info, _validate_member(info)) for info in infos]
            for info, relative in validated:
                if info.is_dir():
                    (destination / Path(*relative.parts)).mkdir(parents=True, exist_ok=True)
                    continue
                enforce_download_budget(file_size=info.file_size, announcement_bytes=running_total)
                target = (destination / Path(*relative.parts)).resolve()
                if not target.is_relative_to(destination):
                    raise AttachmentRejected("ZIP_UNSAFE_PATH", info.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                created_paths.append(target)
                written = 0
                digest_parts: list[bytes] = []
                with bundle.open(info) as source, target.open("xb") as output:
                    while chunk := source.read(64 * 1024):
                        written += len(chunk)
                        if (
                            written > MAX_FILE_BYTES
                            or running_total + written > MAX_ANNOUNCEMENT_BYTES
                        ):
                            raise AttachmentRejected("ZIP_EXPANDED_LIMIT_EXCEEDED", info.filename)
                        output.write(chunk)
                        digest_parts.append(chunk)
                running_total += written
                extracted.append(
                    StoredAttachment(
                        relative_path=relative.as_posix(),
                        size_bytes=written,
                        sha256=sha256_bytes(b"".join(digest_parts)),
                        nested_archive=relative.suffix.casefold() == ".zip",
                    )
                )
    except zipfile.BadZipFile as exc:
        raise AttachmentRejected("ZIP_CORRUPT", "Invalid ZIP archive") from exc
    except Exception:
        for path in created_paths:
            path.unlink(missing_ok=True)
        raise
    return tuple(extracted)


def store_attachment(
    source: Path, target: Path, *, announcement_bytes: int = 0
) -> StoredAttachment:
    """Copy a bounded attachment without executing or interpreting its contents."""

    size = source.stat().st_size
    enforce_download_budget(file_size=size, announcement_bytes=announcement_bytes)
    if source.suffix.casefold() in _EXECUTABLE_SUFFIXES:
        raise AttachmentRejected("EXECUTABLE_FILE", "Executable attachment rejected")
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_file, target.open("xb") as output_file:
        shutil.copyfileobj(input_file, output_file, length=64 * 1024)
    data = target.read_bytes()
    os.chmod(target, 0o600)
    return StoredAttachment(
        target.name, size, sha256_bytes(data), target.suffix.casefold() == ".zip"
    )


def detected_format(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.casefold()
    if data.startswith(b"%PDF-"):
        detected = "PDF"
    elif data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        detected = "HWP"
    elif data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
                if "word/document.xml" in names:
                    detected = "DOCX"
                elif any(name.startswith("xl/") for name in names):
                    detected = "XLSX"
                elif any(name.startswith("Contents/section") for name in names):
                    detected = "HWPX"
                else:
                    detected = "ZIP"
        except zipfile.BadZipFile:
            detected = "UNKNOWN"
    elif data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff"):
        detected = "IMAGE"
    else:
        detected = "UNKNOWN"
    expected = {
        ".pdf": "PDF",
        ".hwp": "HWP",
        ".hwpx": "HWPX",
        ".docx": "DOCX",
        ".xlsx": "XLSX",
        ".zip": "ZIP",
        ".png": "IMAGE",
        ".jpg": "IMAGE",
        ".jpeg": "IMAGE",
    }.get(suffix)
    if expected and detected != expected:
        raise AttachmentRejected("MIME_EXTENSION_MISMATCH", f"Expected {expected}, got {detected}")
    return detected
