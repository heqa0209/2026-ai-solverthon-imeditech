from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.pipeline.bizinfo import BizinfoPage


@dataclass
class DailyStopDetector:
    """Stop only after two consecutive old pages contain no changed item hash."""

    last_success_at: datetime
    known_hashes: dict[str, str]
    consecutive_unchanged_old_pages: int = 0

    def observe(self, page: BizinfoPage) -> bool:
        all_old = bool(page.items) and all(
            item.published_on is not None and item.published_on < self.last_success_at.date()
            for item in page.items
        )
        all_unchanged = bool(page.items) and all(
            self.known_hashes.get(item.source_id) == item.raw_hash for item in page.items
        )
        if all_old and all_unchanged:
            self.consecutive_unchanged_old_pages += 1
        else:
            self.consecutive_unchanged_old_pages = 0
        return self.consecutive_unchanged_old_pages >= 2


def unavailable_after_two_full_snapshots(
    *, known_source_ids: set[str], previous_snapshot: set[str], current_snapshot: set[str]
) -> set[str]:
    """A source becomes unavailable only after absence from two complete snapshots."""

    return known_source_ids - (previous_snapshot | current_snapshot)
