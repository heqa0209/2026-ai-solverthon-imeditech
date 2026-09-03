from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.enums import Verdict
from app.pipeline.hashing import sha256_bytes


class StrictHoldoutModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HoldoutCase(StrictHoldoutModel):
    case_id: str
    company_profile_path: str
    announcement_path: str
    expected_verdict: Verdict
    evidence: list[str]


class HoldoutManifest(StrictHoldoutModel):
    frozen_at: datetime
    authored_without_pipeline_access: bool
    cases: list[HoldoutCase]
    corpus_sha256: str


@dataclass(frozen=True)
class HoldoutReport:
    passed: bool
    sample_size: int
    missed_known_eligible: int
    ineligible_published_eligible: int
    reason: str | None


def load_holdout_manifest(path: Path) -> HoldoutManifest:
    manifest = HoldoutManifest.model_validate_json(path.read_text(encoding="utf-8"))
    root = path.parent.resolve()
    corpus = bytearray()
    for case in sorted(manifest.cases, key=lambda item: item.case_id):
        for relative in (case.company_profile_path, case.announcement_path):
            source = (root / relative).resolve()
            if not source.is_relative_to(root):
                raise ValueError("Holdout path escapes manifest directory")
            corpus.extend(source.read_bytes())
    if sha256_bytes(bytes(corpus)) != manifest.corpus_sha256:
        raise ValueError("Holdout corpus hash mismatch")
    return manifest


def evaluate_holdout(
    manifest: HoldoutManifest, actual_verdicts: dict[str, Verdict]
) -> HoldoutReport:
    missed = 0
    false_eligible = 0
    for case in manifest.cases:
        actual = actual_verdicts.get(case.case_id)
        if case.expected_verdict is Verdict.ELIGIBLE and actual is not Verdict.ELIGIBLE:
            missed += 1
        if case.expected_verdict is Verdict.INELIGIBLE and actual is Verdict.ELIGIBLE:
            false_eligible += 1
    reason = None
    if len(manifest.cases) < 5:
        reason = "HOLDOUT_SAMPLE_TOO_SMALL"
    elif not manifest.authored_without_pipeline_access:
        reason = "HOLDOUT_NOT_INDEPENDENT"
    elif set(actual_verdicts) != {case.case_id for case in manifest.cases}:
        reason = "HOLDOUT_RESULTS_INCOMPLETE"
    passed = reason is None and missed == 0 and false_eligible == 0
    return HoldoutReport(passed, len(manifest.cases), missed, false_eligible, reason)
