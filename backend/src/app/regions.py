from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from app.schemas import RegionInput, RegionItem

REGION_DATA_PATH = Path(__file__).parent / "data/legal-regions-20260720.json"
SAFE_ALIASES = {
    "서울": "서울특별시",
    "부산": "부산광역시",
    "대구": "대구광역시",
    "인천": "인천광역시",
    "광주": "광주광역시",
    "대전": "대전광역시",
    "울산": "울산광역시",
    "세종": "세종특별자치시",
    "경기": "경기도",
    "강원": "강원특별자치도",
    "충북": "충청북도",
    "충남": "충청남도",
    "전북": "전북특별자치도",
    "전남": "전라남도",
    "경북": "경상북도",
    "경남": "경상남도",
    "제주": "제주특별자치도",
}


def _search_form(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


@lru_cache(maxsize=1)
def load_regions() -> tuple[RegionItem, ...]:
    if not REGION_DATA_PATH.is_file():
        return ()
    raw = json.loads(REGION_DATA_PATH.read_text(encoding="utf-8"))
    return tuple(RegionItem.model_validate(item) for item in raw["regions"])


def search_regions(query: str, *, limit: int = 50) -> list[RegionItem]:
    query = query.strip()
    if not query:
        return []
    parts = query.split(maxsplit=1)
    normalized = SAFE_ALIASES.get(parts[0], parts[0])
    if len(parts) == 2:
        normalized = f"{normalized} {parts[1]}"
    needle = _search_form(normalized)
    exact: list[RegionItem] = []
    prefix: list[RegionItem] = []
    contains: list[RegionItem] = []
    for region in load_regions():
        name = _search_form(region.name)
        qualified = _search_form(f"{region.parentName or ''} {region.name}")
        target = (
            exact
            if name == needle or qualified == needle
            else prefix
            if name.startswith(needle)
            else contains
        )
        if needle in name or needle in qualified:
            target.append(region)
    return (exact + prefix + contains)[:limit]


def validate_regions(regions: list[RegionInput]) -> None:
    catalog = {region.code: region.name for region in load_regions()}
    if not catalog and regions:
        raise ValueError("canonical region data is unavailable")
    invalid = [region.code for region in regions if catalog.get(region.code) != region.name]
    if invalid:
        raise ValueError(f"unknown or mismatched region codes: {', '.join(invalid)}")


def region_matches(actual_codes: set[str], expected_codes: set[str]) -> bool:
    if actual_codes & expected_codes:
        return True
    catalog = {region.code: region for region in load_regions()}
    for actual in actual_codes:
        actual_region = catalog.get(actual)
        for expected in expected_codes:
            expected_region = catalog.get(expected)
            if actual_region and expected_region:
                if actual_region.parentCode == expected or expected_region.parentCode == actual:
                    return True
    return False
