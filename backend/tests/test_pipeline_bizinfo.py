from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.pipeline.bizinfo import BizinfoClient, BizinfoPayloadError, parse_bizinfo_page
from app.pipeline.hashing import (
    announcement_content_hash,
    build_version_candidate,
    sha256_json,
)

FIXTURES = Path(__file__).parent / "fixtures" / "bizinfo"


def test_official_wrapper_fixture_is_preserved_and_parsed() -> None:
    payload = json.loads((FIXTURES / "page.json").read_text(encoding="utf-8"))

    page = parse_bizinfo_page(payload, page_index=1, page_unit=100)

    assert page.raw_payload == payload
    assert page.raw_hash == sha256_json(payload)
    assert page.total_count == 1
    item = page.items[0]
    assert item.source_id == "PBLN-2026-001"
    assert item.recruitment_starts_on == date(2026, 9, 1)
    assert item.recruitment_ends_on == date(2026, 9, 30)
    assert item.attachments[0].name == "공고문.pdf"
    assert item.attachments[0].size_bytes == 1024


def test_parser_rejects_items_without_stable_identity() -> None:
    with pytest.raises(BizinfoPayloadError):
        parse_bizinfo_page({"jsonArray": [{"pblancNm": "missing"}]}, page_index=1, page_unit=1)


@pytest.mark.asyncio
async def test_client_uses_only_official_query_contract() -> None:
    payload = json.loads((FIXTURES / "page.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {
            "crtfcKey": "fixture-key",
            "dataType": "json",
            "pageUnit": "20",
            "pageIndex": "2",
        }
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        page = await BizinfoClient("fixture-key", http).fetch_page(page_index=2, page_unit=20)
    assert page.page_index == 2


def test_version_hash_is_stable_and_changes_with_any_source_input() -> None:
    raw = {"b": 2, "a": 1}
    first = announcement_content_hash(raw, "body", ["b", "a"])
    assert first == announcement_content_hash({"a": 1, "b": 2}, "body", ["a", "b"])
    assert first != announcement_content_hash(raw, "changed", ["a", "b"])
    candidate = build_version_candidate(
        source_id="id",
        source_url="https://example.test/id",
        raw_payload=raw,
        body_text="body",
        attachment_hashes=["b", "a"],
    )
    assert candidate.content_hash == first
    assert candidate.attachment_hashes == ("a", "b")
