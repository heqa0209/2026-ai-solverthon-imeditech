from __future__ import annotations

from datetime import UTC, datetime

from app.pipeline.bizinfo import parse_bizinfo_page
from app.pipeline.collection import DailyStopDetector, unavailable_after_two_full_snapshots


def _page(source_id: str, published: str):
    return parse_bizinfo_page(
        {
            "jsonArray": [
                {
                    "pblancId": source_id,
                    "pblancNm": "fixture",
                    "pblancUrl": f"https://example.test/{source_id}",
                    "creatPnttm": published,
                }
            ]
        },
        page_index=1,
        page_unit=1,
    )


def test_daily_collection_requires_two_consecutive_old_unchanged_pages() -> None:
    first = _page("old-1", "20260801")
    second = _page("old-2", "20260802")
    detector = DailyStopDetector(
        last_success_at=datetime(2026, 9, 1, tzinfo=UTC),
        known_hashes={
            "old-1": first.items[0].raw_hash,
            "old-2": second.items[0].raw_hash,
        },
    )
    assert detector.observe(first) is False
    assert detector.observe(second) is True
    changed = _page("old-2", "20260803")
    assert detector.observe(changed) is False


def test_source_unavailable_requires_absence_from_two_complete_snapshots() -> None:
    result = unavailable_after_two_full_snapshots(
        known_source_ids={"still", "missing", "returned"},
        previous_snapshot={"still", "returned"},
        current_snapshot={"still"},
    )
    assert result == {"missing"}
