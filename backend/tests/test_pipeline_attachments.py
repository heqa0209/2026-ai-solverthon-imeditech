from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path

import pytest

from app.pipeline.attachments import (
    MAX_ANNOUNCEMENT_BYTES,
    MAX_FILE_BYTES,
    AttachmentRejected,
    detected_format,
    enforce_download_budget,
    safe_extract_zip,
)


def _archive(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)


@pytest.mark.parametrize(
    ("size", "used", "code"),
    [
        (MAX_FILE_BYTES + 1, 0, "FILE_LIMIT_EXCEEDED"),
        (1, MAX_ANNOUNCEMENT_BYTES, "ANNOUNCEMENT_LIMIT_EXCEEDED"),
    ],
)
def test_attachment_size_limits(size: int, used: int, code: str) -> None:
    with pytest.raises(AttachmentRejected) as error:
        enforce_download_budget(file_size=size, announcement_bytes=used)
    assert error.value.code == code


@pytest.mark.parametrize("name", ["../escape.txt", "/absolute.txt", "C:\\escape.txt", "run.sh"])
def test_zip_rejects_traversal_absolute_and_executable_paths(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "bad.zip"
    _archive(archive, {name: b"unsafe"})
    with pytest.raises(AttachmentRejected):
        safe_extract_zip(archive, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_zip_rejects_symlink(tmp_path: Path) -> None:
    archive = tmp_path / "link.zip"
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(info, "target")
    with pytest.raises(AttachmentRejected) as error:
        safe_extract_zip(archive, tmp_path / "out")
    assert error.value.code == "ZIP_SYMLINK"


def test_nested_zip_is_stored_but_marked_without_recursive_extraction(tmp_path: Path) -> None:
    archive = tmp_path / "outer.zip"
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as inner:
        inner.writestr("inside.txt", "data")
    _archive(archive, {"docs/readme.txt": b"hello", "nested.zip": nested.getvalue()})
    stored = safe_extract_zip(archive, tmp_path / "out")
    assert [item.relative_path for item in stored] == ["docs/readme.txt", "nested.zip"]
    assert stored[1].nested_archive is True
    assert not (tmp_path / "out" / "inside.txt").exists()


def test_signature_must_match_extension() -> None:
    with pytest.raises(AttachmentRejected) as error:
        detected_format("notice.pdf", b"PK\x03\x04garbage")
    assert error.value.code == "MIME_EXTENSION_MISMATCH"


def test_zip_based_document_signature_is_identified() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml", "<document />")
    assert detected_format("notice.docx", output.getvalue()) == "DOCX"
