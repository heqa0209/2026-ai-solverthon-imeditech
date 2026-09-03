from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.pipeline.extraction import (
    NativeExtraction,
    TextSegment,
    decide_ocr,
    extract_native,
)


@pytest.mark.parametrize(
    ("suffix", "member", "xml", "expected"),
    [
        (
            ".docx",
            "word/document.xml",
            "<w:document xmlns:w='w'><w:t>공고 본문</w:t></w:document>",
            "공고 본문",
        ),
        (".xlsx", "xl/sharedStrings.xml", "<sst><si><t>매출 기준</t></si></sst>", "매출 기준"),
        (
            ".hwpx",
            "Contents/section0.xml",
            "<section><text>지역 조건</text></section>",
            "지역 조건",
        ),
    ],
)
def test_zip_xml_native_extractors_preserve_filename_location(
    tmp_path: Path, suffix: str, member: str, xml: str, expected: str
) -> None:
    path = tmp_path / f"notice{suffix}"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, xml)
    result = extract_native(path)
    assert expected in result.text
    assert result.segments[0].filename == path.name


def test_partial_pdf_native_text_suppresses_ocr_by_approved_policy(tmp_path: Path) -> None:
    path = tmp_path / "mixed.pdf"
    extraction = NativeExtraction((TextSegment("one native page", page=1),), "PDF")
    decision = decide_ocr(path, extraction)
    assert decision.required is False
    assert decision.reason == "NATIVE_TEXT_AVAILABLE"


@pytest.mark.parametrize("suffix", [".pdf", ".png", ".jpg", ".hwp"])
def test_empty_ocr_capable_format_requests_ocr(tmp_path: Path, suffix: str) -> None:
    decision = decide_ocr(tmp_path / f"scan{suffix}", NativeExtraction((), suffix[1:].upper()))
    assert decision.required is True
    assert decision.reason == "NO_NATIVE_TEXT"
