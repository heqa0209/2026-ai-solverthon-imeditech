from __future__ import annotations

from datetime import UTC, datetime

from app.enums import Verdict
from app.pipeline.holdout import HoldoutCase, HoldoutManifest, evaluate_holdout


def _manifest(count: int, *, independent: bool = True) -> HoldoutManifest:
    return HoldoutManifest(
        frozen_at=datetime.now(UTC),
        authored_without_pipeline_access=independent,
        corpus_sha256="0" * 64,
        cases=[
            HoldoutCase(
                case_id=f"case-{index}",
                company_profile_path=f"company-{index}.json",
                announcement_path=f"announcement-{index}.json",
                expected_verdict=(Verdict.ELIGIBLE if index == 0 else Verdict.INELIGIBLE),
                evidence=["official source line"],
            )
            for index in range(count)
        ],
    )


def test_holdout_requires_at_least_five_independent_complete_cases() -> None:
    small = _manifest(4)
    actual = {case.case_id: case.expected_verdict for case in small.cases}
    report = evaluate_holdout(small, actual)
    assert report.passed is False
    assert report.reason == "HOLDOUT_SAMPLE_TOO_SMALL"

    dependent = _manifest(5, independent=False)
    actual = {case.case_id: case.expected_verdict for case in dependent.cases}
    report = evaluate_holdout(dependent, actual)
    assert report.passed is False
    assert report.reason == "HOLDOUT_NOT_INDEPENDENT"


def test_holdout_reports_misses_and_false_eligible_separately() -> None:
    manifest = _manifest(5)
    actual = {case.case_id: Verdict.NEEDS_CONFIRMATION for case in manifest.cases}
    actual["case-1"] = Verdict.ELIGIBLE
    report = evaluate_holdout(manifest, actual)
    assert report.passed is False
    assert report.missed_known_eligible == 1
    assert report.ineligible_published_eligible == 1
