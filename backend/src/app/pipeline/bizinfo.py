from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from app.pipeline.hashing import sha256_json

BIZINFO_ENDPOINT = "https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do"


class BizinfoPayloadError(ValueError):
    pass


def _first(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _text(item: dict[str, Any], *keys: str) -> str | None:
    value = _first(item, *keys)
    return str(value).strip() if value is not None else None


def _date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    normalized = "".join(ch for ch in str(value) if ch.isdigit())
    if len(normalized) < 8:
        return None
    normalized = normalized[:8]
    try:
        return date.fromisoformat(f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:]}")
    except ValueError:
        return None


def _recruitment_dates(item: dict[str, Any]) -> tuple[date | None, date | None]:
    start = _date(_first(item, "reqstBeginDe", "recruitmentStartDate", "beginDate"))
    end = _date(_first(item, "reqstEndDe", "recruitmentEndDate", "endDate"))
    if start or end:
        return start, end
    period = _text(item, "reqstBeginEndDe", "recruitmentPeriod")
    if not period:
        return None, None
    chunks = [part for part in period.replace("~", " ").split() if any(c.isdigit() for c in part)]
    parsed = [_date(chunk) for chunk in chunks]
    parsed = [value for value in parsed if value is not None]
    return (parsed[0] if parsed else None, parsed[1] if len(parsed) > 1 else None)


@dataclass(frozen=True)
class AttachmentMetadata:
    name: str
    url: str
    size_bytes: int | None
    source_order: int


@dataclass(frozen=True)
class ParsedAnnouncement:
    source_id: str
    source_url: str
    title: str
    agency_name: str | None
    summary_text: str | None
    body_text: str | None
    published_on: date | None
    recruitment_starts_on: date | None
    recruitment_ends_on: date | None
    attachments: tuple[AttachmentMetadata, ...]
    raw_item: dict[str, Any]
    raw_hash: str


@dataclass(frozen=True)
class BizinfoPage:
    items: tuple[ParsedAnnouncement, ...]
    total_count: int | None
    page_index: int
    page_unit: int
    raw_payload: dict[str, Any]
    raw_hash: str


def _items_from_wrapper(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: Any = payload.get("jsonArray")
    if candidates is None:
        candidates = payload.get("items")
    if isinstance(candidates, dict):
        candidates = candidates.get("item", candidates.get("items"))
    if candidates is None and isinstance(payload.get("response"), dict):
        body = payload["response"].get("body", {})
        candidates = body.get("items", {})
        if isinstance(candidates, dict):
            candidates = candidates.get("item", [])
    if candidates is None:
        candidates = []
    if isinstance(candidates, dict):
        candidates = [candidates]
    if not isinstance(candidates, list) or not all(isinstance(item, dict) for item in candidates):
        raise BizinfoPayloadError("Bizinfo item wrapper must contain an object list")
    return candidates


def _attachments(item: dict[str, Any]) -> tuple[AttachmentMetadata, ...]:
    raw = _first(item, "attachments", "files", "fileList")
    if raw is None:
        names = _text(item, "fileNm", "atchFileNm")
        urls = _text(item, "fileCours", "atchFileUrl")
        if not names or not urls:
            return ()
        raw = [
            {"name": name.strip(), "url": url.strip()}
            for name, url in zip(names.split(","), urls.split(","), strict=False)
        ]
    if isinstance(raw, dict):
        raw = raw.get("item", [raw])
    if not isinstance(raw, list):
        return ()
    result: list[AttachmentMetadata] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        name = _text(entry, "name", "fileNm", "filename")
        url = _text(entry, "url", "fileCours", "downloadUrl")
        if not name or not url:
            continue
        size = _first(entry, "size", "sizeBytes", "fileSize")
        try:
            parsed_size = int(size) if size not in (None, "") else None
        except TypeError, ValueError:
            parsed_size = None
        result.append(AttachmentMetadata(name, url, parsed_size, index))
    return tuple(result)


def parse_bizinfo_page(payload: dict[str, Any], *, page_index: int, page_unit: int) -> BizinfoPage:
    """Parse a preserved official response wrapper without mutating it."""

    if not isinstance(payload, dict):
        raise BizinfoPayloadError("Bizinfo response must be an object")
    parsed: list[ParsedAnnouncement] = []
    for item in _items_from_wrapper(payload):
        source_url = _text(item, "pblancUrl", "pblancDtlUrl", "sourceUrl", "viewUrl")
        source_id = _text(item, "pblancId", "announcementId", "id") or source_url
        title = _text(item, "pblancNm", "title", "announcementName")
        if not source_id or not source_url or not title:
            raise BizinfoPayloadError("Each Bizinfo item requires id/url/title")
        starts_on, ends_on = _recruitment_dates(item)
        parsed.append(
            ParsedAnnouncement(
                source_id=source_id,
                source_url=source_url,
                title=title,
                agency_name=_text(item, "jrsdInsttNm", "agencyName", "organizationName"),
                summary_text=_text(item, "bsnsSumryCn", "summary", "description"),
                body_text=_text(item, "pblancCn", "body", "content"),
                published_on=_date(_first(item, "creatPnttm", "publishedOn", "registDe")),
                recruitment_starts_on=starts_on,
                recruitment_ends_on=ends_on,
                attachments=_attachments(item),
                raw_item=dict(item),
                raw_hash=sha256_json(item),
            )
        )
    total = _first(payload, "totalCount", "totCnt")
    try:
        total_count = int(total) if total not in (None, "") else None
    except TypeError, ValueError:
        total_count = None
    return BizinfoPage(
        items=tuple(parsed),
        total_count=total_count,
        page_index=page_index,
        page_unit=page_unit,
        raw_payload=payload,
        raw_hash=sha256_json(payload),
    )


class BizinfoClient:
    def __init__(
        self,
        api_key: str,
        client: httpx.AsyncClient,
        *,
        endpoint: str = BIZINFO_ENDPOINT,
    ):
        if not api_key:
            raise ValueError("BIZINFO_API_KEY is required")
        self._api_key = api_key
        self._client = client
        self._endpoint = endpoint

    async def fetch_page(self, *, page_index: int, page_unit: int = 100) -> BizinfoPage:
        response = await self._client.get(
            self._endpoint,
            params={
                "crtfcKey": self._api_key,
                "dataType": "json",
                "pageUnit": page_unit,
                "pageIndex": page_index,
            },
        )
        response.raise_for_status()
        return parse_bizinfo_page(response.json(), page_index=page_index, page_unit=page_unit)
