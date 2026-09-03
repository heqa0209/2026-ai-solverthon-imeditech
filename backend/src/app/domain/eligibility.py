from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from app.enums import ConditionStatus, Verdict
from app.regions import region_matches


@dataclass(frozen=True)
class Evaluation:
    status: ConditionStatus
    used_value: dict[str, object] | None = None
    explanation: str | None = None
    assumption_code: str | None = None


@dataclass(frozen=True)
class DecisionEvaluation:
    verdict: Verdict
    passed_track_key: str | None
    conditions: dict[str, Evaluation]


PROFILE_FIELDS = {
    "BUSINESS_ENTITY_TYPE": "businessEntityType",
    "ORGANIZATION_TYPE": "organizationType",
    "COMPANY_SCALE": "companyScale",
    "FOUNDED_ON": "foundedOn",
    "ELIGIBLE_REGION": "eligibleRegions",
    "PRIMARY_INDUSTRY": "primaryIndustry",
    "SECONDARY_INDUSTRY": "secondaryIndustries",
    "ANNUAL_REVENUE": "annualRevenue",
    "EMPLOYEE_COUNT": "employeeCount",
    "DELINQUENCY_STATUS": "delinquencyStatus",
    "CERTIFICATION": "certifications",
    "SUPPORT_HISTORY": "supportHistory",
    "CAPABILITY_TAG": "capabilityTags",
}


def _expected_value(expected: dict[str, Any] | None) -> Any:
    if not expected:
        return None
    value_type = expected.get("type")
    if value_type == "RANGE":
        return expected.get("value")
    for key in ("value", "values", "codes"):
        if key in expected:
            return expected[key]
    return None


def _compare(operator: str, actual: Any, expected: Any) -> bool | None:
    if operator == "EXISTS":
        return actual not in (None, "", [], {})
    if actual is None or expected is None:
        return None
    if operator == "EQ":
        return actual == expected
    if operator == "NE":
        return actual != expected
    if operator == "IN":
        return actual in expected if isinstance(expected, list) else None
    if operator == "NOT_IN":
        return actual not in expected if isinstance(expected, list) else None
    if operator in {"LT", "LTE", "GT", "GTE"}:
        try:
            return {
                "LT": actual < expected,
                "LTE": actual <= expected,
                "GT": actual > expected,
                "GTE": actual >= expected,
            }[operator]
        except TypeError:
            return None
    if operator == "BETWEEN" and isinstance(expected, dict):
        minimum = expected.get("minimum", expected.get("min"))
        maximum = expected.get("maximum", expected.get("max"))
        if minimum is None or maximum is None:
            return None
        include_min = expected.get("includeMinimum", True)
        include_max = expected.get("includeMaximum", True)
        try:
            lower = actual >= minimum if include_min else actual > minimum
            upper = actual <= maximum if include_max else actual < maximum
            return lower and upper
        except TypeError:
            return None
    if operator in {"CONTAINS", "NOT_CONTAINS"}:
        if isinstance(actual, str) and isinstance(expected, str):
            result = expected.casefold() in actual.casefold()
        elif isinstance(actual, list):
            folded = {str(value).casefold() for value in actual}
            result = str(expected).casefold() in folded
        else:
            return None
        return result if operator == "CONTAINS" else not result
    return None


def _profile_value(profile: dict[str, Any], subject: str) -> tuple[Any, str | None]:
    field = PROFILE_FIELDS.get(subject)
    if field is None:
        return None, None
    value = profile.get(field)
    assumption = None
    if subject == "ELIGIBLE_REGION" and value:
        value = [item.get("code") for item in value if isinstance(item, dict)]
        assumption = "ELIGIBLE_REGION_LOCATION_TYPE_SUBSTITUTION"
    elif subject == "COMPANY_SCALE" and value:
        assumption = "COMPANY_SCALE_USER_ASSERTED"
    elif subject == "CERTIFICATION" and value:
        assumption = "CERTIFICATION_VALIDITY_ASSUMED"
    return value, assumption


def evaluate_condition(profile: dict[str, Any], condition: dict[str, Any]) -> Evaluation:
    subject = str(condition.get("subject", "OTHER"))
    operator = str(condition.get("operator", ""))
    expected = _expected_value(condition.get("expected_value") or condition.get("expectedValue"))
    actual, assumption = _profile_value(profile, subject)

    if subject == "OTHER" or operator == "SEMANTIC_MATCH":
        return Evaluation(ConditionStatus.UNKNOWN, explanation="비정형 의미판단이 필요합니다.")

    if subject == "FOUNDED_ON" and isinstance(actual, str):
        try:
            actual = date.fromisoformat(actual)
        except ValueError:
            actual = None
    if subject == "FOUNDED_ON" and isinstance(expected, str):
        try:
            expected = date.fromisoformat(expected)
        except ValueError:
            return Evaluation(ConditionStatus.UNKNOWN, explanation="기준일을 변환할 수 없습니다.")

    if subject in {"ANNUAL_REVENUE", "EMPLOYEE_COUNT"} and condition.get("reference_date"):
        assumption = (
            "ANNUAL_REVENUE_REFERENCE_PERIOD_SUBSTITUTION"
            if subject == "ANNUAL_REVENUE"
            else "EMPLOYEE_COUNT_REFERENCE_DATE_SUBSTITUTION"
        )

    if subject == "ELIGIBLE_REGION":
        expected_codes = set(expected or []) if isinstance(expected, list) else set()
        actual_codes = set(actual or []) if isinstance(actual, list) else set()
        if not actual_codes or not expected_codes:
            result = None
        else:
            matched = region_matches(actual_codes, expected_codes)
            result = not matched if operator in {"NOT_IN", "NOT_CONTAINS"} else matched
    elif subject in {
        "SECONDARY_INDUSTRY",
        "CERTIFICATION",
        "SUPPORT_HISTORY",
        "CAPABILITY_TAG",
    }:
        if subject == "SUPPORT_HISTORY" and isinstance(actual, list):
            values = [item.get("programName", "") for item in actual if isinstance(item, dict)]
        else:
            values = actual
        result = _compare(operator, values, expected)
    else:
        result = _compare(operator, actual, expected)

    used = None if actual is None else {"value": actual}
    if result is None:
        return Evaluation(
            ConditionStatus.UNKNOWN,
            used_value=used,
            explanation="비교할 값이 없거나 조건을 안전하게 변환할 수 없습니다.",
            assumption_code=assumption,
        )
    return Evaluation(
        ConditionStatus.PASS if result else ConditionStatus.FAIL,
        used_value=used,
        explanation="조건을 충족합니다." if result else "명시된 조건과 일치하지 않습니다.",
        assumption_code=assumption,
    )


def aggregate(operator: str, statuses: list[ConditionStatus]) -> ConditionStatus:
    applicable = [status for status in statuses if status != ConditionStatus.NOT_APPLICABLE]
    if not applicable:
        return ConditionStatus.UNKNOWN
    if operator == "ANY":
        if ConditionStatus.PASS in applicable:
            return ConditionStatus.PASS
        if ConditionStatus.UNKNOWN in applicable:
            return ConditionStatus.UNKNOWN
        return ConditionStatus.FAIL
    if ConditionStatus.FAIL in applicable:
        return ConditionStatus.FAIL
    if ConditionStatus.UNKNOWN in applicable:
        return ConditionStatus.UNKNOWN
    return ConditionStatus.PASS


def verdict_from_paths(statuses: list[ConditionStatus]) -> Verdict:
    if ConditionStatus.PASS in statuses:
        return Verdict.ELIGIBLE
    if ConditionStatus.UNKNOWN in statuses or not statuses:
        return Verdict.NEEDS_CONFIRMATION
    return Verdict.INELIGIBLE


def evaluate_decision(
    canonical_ir: dict[str, Any],
    profile: dict[str, Any],
    selected_role_key: str | None = None,
    condition_values: dict[str, object] | None = None,
) -> DecisionEvaluation:
    conditions = [item for item in canonical_ir.get("conditions", []) if isinstance(item, dict)]
    mandatory = [item for item in conditions if item.get("kind") == "MANDATORY"]

    def role_selection_required() -> DecisionEvaluation:
        evaluations = {
            str(item.get("condition_id") or item.get("conditionId")): Evaluation(
                ConditionStatus.NOT_APPLICABLE
            )
            for item in mandatory
        }
        return DecisionEvaluation(Verdict.NEEDS_CONFIRMATION, None, evaluations)

    roles = canonical_ir.get("roles", [])
    role_keys = {
        role.get("role_key") for role in roles if isinstance(role, dict) and role.get("role_key")
    }
    if role_keys and selected_role_key is None:
        return role_selection_required()
    if selected_role_key is not None and selected_role_key not in role_keys:
        return role_selection_required()

    groups = [item for item in canonical_ir.get("groups", []) if isinstance(item, dict)]
    group_by_id = {str(item.get("group_id") or item.get("groupId")): item for item in groups}

    def group_applies(group_id: str, visiting: set[str] | None = None) -> bool:
        visiting = set() if visiting is None else visiting
        if group_id in visiting:
            return False
        group = group_by_id.get(group_id)
        if group is None:
            return True
        applicable_roles = group.get("role_keys", [])
        if applicable_roles and selected_role_key not in applicable_roles:
            return False
        parent = group.get("parent_group_id") or group.get("parentGroupId")
        return parent is None or group_applies(str(parent), visiting | {group_id})

    evaluations: dict[str, Evaluation] = {}
    applicable_conditions: list[dict[str, Any]] = []
    for condition in mandatory:
        condition_id = str(condition.get("condition_id") or condition.get("conditionId"))
        group_id = str(condition.get("group_id") or condition.get("groupId"))
        if not group_applies(group_id):
            evaluations[condition_id] = Evaluation(ConditionStatus.NOT_APPLICABLE)
            continue
        condition_profile = profile
        if condition_values and condition_id in condition_values:
            field = PROFILE_FIELDS.get(str(condition.get("subject", "OTHER")))
            if field:
                condition_profile = {**profile, field: condition_values[condition_id]}
        evaluations[condition_id] = evaluate_condition(condition_profile, condition)
        applicable_conditions.append(condition)

    condition_groups: dict[str, list[ConditionStatus]] = {}
    for condition in applicable_conditions:
        condition_id = str(condition.get("condition_id") or condition.get("conditionId"))
        group_id = str(condition.get("group_id") or condition.get("groupId"))
        condition_groups.setdefault(group_id, []).append(evaluations[condition_id].status)

    group_statuses: dict[str, ConditionStatus] = {}

    def group_status(group_id: str, visiting: set[str]) -> ConditionStatus:
        if group_id in group_statuses:
            return group_statuses[group_id]
        if group_id in visiting:
            return ConditionStatus.UNKNOWN
        group = group_by_id.get(group_id, {})
        statuses = list(condition_groups.get(group_id, []))
        for child_id, child in group_by_id.items():
            parent = child.get("parent_group_id") or child.get("parentGroupId")
            if parent is not None and str(parent) == group_id and group_applies(child_id):
                statuses.append(group_status(child_id, visiting | {group_id}))
        status = aggregate(str(group.get("operator", "ALL")), statuses)
        group_statuses[group_id] = status
        return status

    roots = [
        (group_id, group)
        for group_id, group in group_by_id.items()
        if not (group.get("parent_group_id") or group.get("parentGroupId"))
        and group_applies(group_id)
    ]
    path_statuses: list[ConditionStatus] = []
    passed_track = None
    for group_id, group in roots:
        status = group_status(group_id, set())
        path_statuses.append(status)
        if status == ConditionStatus.PASS and passed_track is None:
            track_ids = group.get("track_ids", [])
            passed_track = track_ids[0] if len(track_ids) == 1 else None

    if not roots and applicable_conditions:
        path_statuses = [
            aggregate(
                "ALL",
                [
                    evaluations[str(item.get("condition_id") or item.get("conditionId"))].status
                    for item in applicable_conditions
                ],
            )
        ]
    return DecisionEvaluation(verdict_from_paths(path_statuses), passed_track, evaluations)
