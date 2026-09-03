import json
from pathlib import Path

import pytest

from app import regions
from app.schemas import RegionInput


@pytest.fixture
def region_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "regions.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": "1",
                "effectiveDate": "2026-07-20",
                "regions": [
                    {
                        "code": "11",
                        "name": "서울특별시",
                        "parentCode": None,
                        "parentName": None,
                        "level": "SIDO",
                    },
                    {
                        "code": "11110",
                        "name": "종로구",
                        "parentCode": "11",
                        "parentName": "서울특별시",
                        "level": "SIGUNGU",
                    },
                    {
                        "code": "11140",
                        "name": "중구",
                        "parentCode": "11",
                        "parentName": "서울특별시",
                        "level": "SIGUNGU",
                    },
                    {
                        "code": "26110",
                        "name": "중구",
                        "parentCode": "26",
                        "parentName": "부산광역시",
                        "level": "SIGUNGU",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(regions, "REGION_DATA_PATH", path)
    regions.load_regions.cache_clear()
    yield
    regions.load_regions.cache_clear()


def test_region_search_supports_safe_alias_and_returns_ambiguous_candidates(
    region_catalog: None,
) -> None:
    assert [item.code for item in regions.search_regions("서울")][0] == "11"
    assert [item.code for item in regions.search_regions("중구")] == ["11140", "26110"]
    assert [item.code for item in regions.search_regions("서울 중구")] == ["11140"]


def test_region_validation_requires_exact_code_name_pair(region_catalog: None) -> None:
    regions.validate_regions([RegionInput(code="11110", name="종로구")])
    with pytest.raises(ValueError):
        regions.validate_regions([RegionInput(code="11110", name="중구")])


def test_sido_selection_includes_child_region(region_catalog: None) -> None:
    assert regions.region_matches({"11110"}, {"11"})
    assert regions.region_matches({"11"}, {"11110"})
