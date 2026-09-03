from __future__ import annotations

from app.domain.eligibility import (
    Evaluation,
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
    revenue["unit"] = "원"
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
    employee["unit"] = "명"
    assert evaluate_condition({}, employee).status == ConditionStatus.UNKNOWN
    assert evaluate_condition({"employeeCount": 11}, employee).status == ConditionStatus.FAIL
    assert (
        evaluate_condition(
            {"primaryIndustry": "의료기기"},
            condition("PRIMARY_INDUSTRY", "SEMANTIC_MATCH", {"type": "STRING", "value": "바이오"}),
        ).status
        == ConditionStatus.UNKNOWN
    )


def test_explicit_semantic_answer_takes_priority_over_stored_semantic_evaluation() -> None:
    semantic = condition(
        "PRIMARY_INDUSTRY",
        "SEMANTIC_MATCH",
        {"type": "STRING", "value": "바이오"},
        key="semantic-fit",
    )
    ir = {
        "groups": [{"group_id": "root", "operator": "ALL"}],
        "conditions": [semantic],
    }

    confirmed = evaluate_decision(
        ir,
        {"primaryIndustry": "의료기기"},
        condition_values={"semantic-fit": True},
        semantic_evaluations={
            "semantic-fit": Evaluation(
                ConditionStatus.FAIL,
                explanation="이전 profile 기준 의미판단",
            )
        },
    )
    rejected = evaluate_decision(
        ir,
        {"primaryIndustry": "바이오"},
        condition_values={"semantic-fit": False},
        semantic_evaluations={"semantic-fit": Evaluation(ConditionStatus.PASS)},
    )

    assert confirmed.verdict == Verdict.ELIGIBLE
    assert confirmed.conditions["semantic-fit"].status == ConditionStatus.PASS
    assert confirmed.conditions["semantic-fit"].used_value == {"value": True}
    assert rejected.verdict == Verdict.INELIGIBLE
    assert rejected.conditions["semantic-fit"].status == ConditionStatus.FAIL


def test_tagged_range_expected_value_is_evaluated_against_inner_bounds() -> None:
    revenue_range = condition(
        "ANNUAL_REVENUE",
        "BETWEEN",
        {"type": "RANGE", "value": {"minimum": 10, "maximum": 20}},
    )
    revenue_range["unit"] = "원"
    assert evaluate_condition({"annualRevenue": 15}, revenue_range).status == ConditionStatus.PASS
    assert evaluate_condition({"annualRevenue": 21}, revenue_range).status == ConditionStatus.FAIL
    assert evaluate_condition({}, revenue_range).status == ConditionStatus.UNKNOWN


def test_numeric_units_must_match_the_company_profile_unit() -> None:
    revenue = condition("ANNUAL_REVENUE", "LTE", {"type": "INTEGER", "value": 100})
    revenue["unit"] = "억원"
    employee = condition("EMPLOYEE_COUNT", "LTE", {"type": "INTEGER", "value": 10})
    employee["unit"] = None

    assert evaluate_condition({"annualRevenue": 90}, revenue).status == ConditionStatus.UNKNOWN
    assert evaluate_condition({"employeeCount": 9}, employee).status == ConditionStatus.UNKNOWN


def test_set_membership_compares_items_instead_of_nested_lists() -> None:
    certification = condition(
        "CERTIFICATION",
        "IN",
        {"type": "STRING_SET", "values": ["벤처기업", "이노비즈"]},
    )
    excluded = condition(
        "SECONDARY_INDUSTRY",
        "NOT_IN",
        {"type": "STRING_SET", "values": ["사행성"]},
    )

    assert (
        evaluate_condition({"certifications": ["벤처기업"]}, certification).status
        == ConditionStatus.PASS
    )
    assert (
        evaluate_condition({"secondaryIndustries": ["제조업"]}, excluded).status
        == ConditionStatus.PASS
    )
    assert (
        evaluate_condition({"secondaryIndustries": ["제조업", "사행성"]}, excluded).status
        == ConditionStatus.FAIL
    )


def test_role_selection_and_multiple_paths() -> None:
    ir = {
        "roles": [{"role_key": "LEAD"}, {"role_key": "PARTNER"}],
        "groups": [
            {
                "group_id": "lead",
                "parent_group_id": None,
                "operator": "ALL",
                "role_keys": ["LEAD"],
            },
            {
                "group_id": "partner",
                "parent_group_id": None,
                "operator": "ALL",
                "role_keys": ["PARTNER"],
            },
        ],
        "conditions": [
            {
                **condition(
                    "COMPANY_SCALE",
                    "EQ",
                    {"type": "ENUM", "value": "SMALL"},
                    key="lead-scale",
                ),
                "group_id": "lead",
            },
            {
                **condition(
                    "COMPANY_SCALE",
                    "EQ",
                    {"type": "ENUM", "value": "MEDIUM"},
                    key="partner-scale",
                ),
                "group_id": "partner",
            },
        ],
    }
    unselected = evaluate_decision(ir, {"companyScale": "SMALL"})
    assert unselected.verdict == Verdict.NEEDS_CONFIRMATION
    assert {result.status for result in unselected.conditions.values()} == {
        ConditionStatus.NOT_APPLICABLE
    }
    selected = evaluate_decision(ir, {"companyScale": "SMALL"}, "LEAD")
    assert selected.verdict == Verdict.ELIGIBLE
    assert selected.conditions["lead-scale"].status == ConditionStatus.PASS
    assert selected.conditions["partner-scale"].status == ConditionStatus.NOT_APPLICABLE
    partner = evaluate_decision(ir, {"companyScale": "SMALL"}, "PARTNER")
    assert partner.verdict == Verdict.INELIGIBLE
    assert partner.conditions["lead-scale"].status == ConditionStatus.NOT_APPLICABLE
    assert partner.conditions["partner-scale"].status == ConditionStatus.FAIL
