from __future__ import annotations

from app.domain.eligibility import (
    aggregate,
    evaluate_condition,
    evaluate_decision,
    verdict_from_paths,
)
from app.enums import ConditionStatus, Verdict


def condition(subject: str, operator: str, expected: dict, *, key: str = "c1") -> dict:
    return {
        "condition_id": key,
        "group_id": "root",
        "kind": "MANDATORY",
        "subject": subject,
        "operator": operator,
        "expected_value": expected,
    }


def test_fail_dominates_unknown_and_only_all_pass_is_eligible() -> None:
    assert aggregate("ALL", [ConditionStatus.FAIL, ConditionStatus.UNKNOWN]) == ConditionStatus.FAIL
    assert (
        verdict_from_paths([ConditionStatus.FAIL, ConditionStatus.UNKNOWN])
        == Verdict.NEEDS_CONFIRMATION
    )
    assert verdict_from_paths([ConditionStatus.FAIL, ConditionStatus.FAIL]) == Verdict.INELIGIBLE
    assert verdict_from_paths([ConditionStatus.FAIL, ConditionStatus.PASS]) == Verdict.ELIGIBLE


def test_numeric_enum_and_region_conditions_are_deterministic() -> None:
    profile = {
        "companyScale": "SMALL",
        "annualRevenue": 90,
        "eligibleRegions": [{"code": "11", "name": "서울특별시"}],
    }
    assert (
        evaluate_condition(
            profile, condition("COMPANY_SCALE", "IN", {"type": "ENUM", "values": ["SMALL"]})
        ).status
        == ConditionStatus.PASS
    )
    revenue = condition("ANNUAL_REVENUE", "LTE", {"type": "INTEGER", "value": 100})
    revenue["reference_date"] = "2025-12-31"
    result = evaluate_condition(profile, revenue)
    assert result.status == ConditionStatus.PASS
    assert result.assumption_code == "ANNUAL_REVENUE_REFERENCE_PERIOD_SUBSTITUTION"
    assert (
        evaluate_condition(
            profile,
            condition("ELIGIBLE_REGION", "IN", {"type": "REGION_SET", "codes": ["11"]}),
        ).status
        == ConditionStatus.PASS
    )


def test_missing_or_unconvertible_is_unknown_and_mismatch_is_fail() -> None:
    employee = condition("EMPLOYEE_COUNT", "LTE", {"type": "INTEGER", "value": 10})
    assert evaluate_condition({}, employee).status == ConditionStatus.UNKNOWN
    assert evaluate_condition({"employeeCount": 11}, employee).status == ConditionStatus.FAIL
    assert (
        evaluate_condition(
            {"primaryIndustry": "의료기기"},
            condition("PRIMARY_INDUSTRY", "SEMANTIC_MATCH", {"type": "STRING", "value": "바이오"}),
        ).status
        == ConditionStatus.UNKNOWN
    )


def test_role_selection_and_multiple_paths() -> None:
    ir = {
        "roles": [{"role_key": "LEAD"}],
        "groups": [{"group_id": "root", "operator": "ALL", "role_key": "LEAD"}],
        "conditions": [
            {
                **condition("COMPANY_SCALE", "EQ", {"type": "ENUM", "value": "SMALL"}),
                "role_key": "LEAD",
            }
        ],
    }
    assert evaluate_decision(ir, {"companyScale": "SMALL"}).verdict == Verdict.NEEDS_CONFIRMATION
    selected = evaluate_decision(ir, {"companyScale": "SMALL"}, "LEAD")
    assert selected.verdict == Verdict.ELIGIBLE
