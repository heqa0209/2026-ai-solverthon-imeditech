from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree


class ExtractionFailure(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TextSegment:
    text: str
    page: int | None = None
    filename: str | None = None


@dataclass(frozen=True)
class NativeExtraction:
    segments: tuple[TextSegment, ...]
    format: str

    @property
    def text(self) -> str:
        return "\n".join(segment.text for segment in self.segments if segment.text.strip())


class NativeExtractor(Protocol):
    def supports(self, path: Path) -> bool: ...

    def extract(self, path: Path) -> NativeExtraction: ...


class PdfExtractor:
    def supports(self, path: Path) -> bool:
        return path.suffix.casefold() == ".pdf"

    def extract(self, path: Path) -> NativeExtraction:
        try:
            from pypdf import PdfReader

            reader = PdfReader(path)
            if reader.is_encrypted:
                raise ExtractionFailure("PDF_ENCRYPTED", "Encrypted PDF is not supported")
            segments = tuple(
                TextSegment(text=text, page=index)
                for index, page in enumerate(reader.pages, start=1)
                if (text := (page.extract_text() or "").strip())
            )
            return NativeExtraction(segments, "PDF")
        except ExtractionFailure:
            raise
        except Exception as exc:
            raise ExtractionFailure("PDF_CORRUPT", "Could not extract PDF text") from exc


def _xml_text(data: bytes) -> str:
    try:
        root = ElementTree.parse(io.BytesIO(data)).getroot()
    except ElementTree.ParseError as exc:
        raise ExtractionFailure("OFFICE_XML_INVALID", "Invalid XML in Office attachment") from exc
    pieces = [text.strip() for element in root.iter() if (text := element.text) and text.strip()]
    return " ".join(pieces)


class ZipXmlExtractor:
    suffix: str
    format: str
    members: tuple[str, ...]

    def supports(self, path: Path) -> bool:
        return path.suffix.casefold() == self.suffix

    def _selected_members(self, names: list[str]) -> list[str]:
        return [name for name in names if any(name.startswith(prefix) for prefix in self.members)]

    def extract(self, path: Path) -> NativeExtraction:
        try:
            with zipfile.ZipFile(path) as archive:
                names = self._selected_members(archive.namelist())
                segments = tuple(
                    TextSegment(text=text, filename=path.name)
                    for name in names
                    if (text := _xml_text(archive.read(name)).strip())
                )
        except zipfile.BadZipFile as exc:
            raise ExtractionFailure(f"{self.format}_CORRUPT", "Invalid ZIP-based document") from exc
        return NativeExtraction(segments, self.format)


class DocxExtractor(ZipXmlExtractor):
    suffix = ".docx"
    format = "DOCX"
    members = ("word/document.xml", "word/header", "word/footer")


class XlsxExtractor(ZipXmlExtractor):
    suffix = ".xlsx"
    format = "XLSX"
    members = ("xl/sharedStrings.xml", "xl/worksheets/")


class HwpxExtractor(ZipXmlExtractor):
    suffix = ".hwpx"
    format = "HWPX"
    members = ("Contents/section", "Contents/header.xml")


DEFAULT_EXTRACTORS: tuple[NativeExtractor, ...] = (
    PdfExtractor(),
    HwpxExtractor(),
    DocxExtractor(),
    XlsxExtractor(),
)


def extract_native(
    path: Path, extractors: tuple[NativeExtractor, ...] = DEFAULT_EXTRACTORS
) -> NativeExtraction:
    for extractor in extractors:
        if extractor.supports(path):
            return extractor.extract(path)
    if path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".hwp"}:
        return NativeExtraction((), path.suffix.removeprefix(".").upper())
    raise ExtractionFailure("UNSUPPORTED_FORMAT", f"No native extractor for {path.suffix}")


@dataclass(frozen=True)
class OcrDecision:
    required: bool
    reason: str


def decide_ocr(path: Path, extraction: NativeExtraction) -> OcrDecision:
    """Apply D-03: any native PDF text suppresses extra page OCR."""

    if extraction.text.strip():
        return OcrDecision(False, "NATIVE_TEXT_AVAILABLE")
    if path.suffix.casefold() in {".pdf", ".png", ".jpg", ".jpeg", ".hwp"}:
        return OcrDecision(True, "NO_NATIVE_TEXT")
    return OcrDecision(False, "OCR_UNSUPPORTED_FOR_FORMAT")
