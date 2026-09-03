from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas import BusinessEntityType, CompanyScale, DelinquencyStatus, OrganizationType


class StrictIRModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GroupOperator(StrEnum):
    ALL = "ALL"
    ANY = "ANY"


class ConditionKind(StrEnum):
    MANDATORY = "MANDATORY"
    PREFERENCE = "PREFERENCE"
    GUIDANCE = "GUIDANCE"
    POST_AWARD = "POST_AWARD"


class Subject(StrEnum):
    BUSINESS_ENTITY_TYPE = "BUSINESS_ENTITY_TYPE"
    ORGANIZATION_TYPE = "ORGANIZATION_TYPE"
    COMPANY_SCALE = "COMPANY_SCALE"
    FOUNDED_ON = "FOUNDED_ON"
    ELIGIBLE_REGION = "ELIGIBLE_REGION"
    PRIMARY_INDUSTRY = "PRIMARY_INDUSTRY"
    SECONDARY_INDUSTRY = "SECONDARY_INDUSTRY"
    ANNUAL_REVENUE = "ANNUAL_REVENUE"
    EMPLOYEE_COUNT = "EMPLOYEE_COUNT"
    DELINQUENCY_STATUS = "DELINQUENCY_STATUS"
    CERTIFICATION = "CERTIFICATION"
    SUPPORT_HISTORY = "SUPPORT_HISTORY"
    CAPABILITY_TAG = "CAPABILITY_TAG"
    OTHER = "OTHER"


class ConditionOperator(StrEnum):
    EQ = "EQ"
    NE = "NE"
    IN = "IN"
    NOT_IN = "NOT_IN"
    LT = "LT"
    LTE = "LTE"
    GT = "GT"
    GTE = "GTE"
    BETWEEN = "BETWEEN"
    CONTAINS = "CONTAINS"
    NOT_CONTAINS = "NOT_CONTAINS"
    EXISTS = "EXISTS"
    SEMANTIC_MATCH = "SEMANTIC_MATCH"


class StringExpected(StrictIRModel):
    type: Literal["STRING"]
    value: str = Field(min_length=1)


class IntegerExpected(StrictIRModel):
    type: Literal["INTEGER"]
    value: int


class DateExpected(StrictIRModel):
    type: Literal["DATE"]
    value: date


class EnumExpected(StrictIRModel):
    type: Literal["ENUM"]
    value: str


class RegionSetExpected(StrictIRModel):
    type: Literal["REGION_SET"]
    value: list[str] = Field(min_length=1)


class StringSetExpected(StrictIRModel):
    type: Literal["STRING_SET"]
    value: list[str] = Field(min_length=1)


class RangeValue(StrictIRModel):
    minimum: int | None
    maximum: int | None

    @model_validator(mode="after")
    def ordered(self) -> RangeValue:
        if self.minimum is None and self.maximum is None:
            raise ValueError("RANGE requires at least one bound")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("RANGE minimum cannot exceed maximum")
        return self


class RangeExpected(StrictIRModel):
    type: Literal["RANGE"]
    value: RangeValue


class BooleanExpected(StrictIRModel):
    type: Literal["BOOLEAN"]
    value: bool


ExpectedValue = Annotated[
    StringExpected
    | IntegerExpected
    | DateExpected
    | EnumExpected
    | RegionSetExpected
    | StringSetExpected
    | RangeExpected
    | BooleanExpected,
    Field(discriminator="type"),
]


_EXPECTED_TYPE_BY_MODEL = {
    StringExpected: "STRING",
    IntegerExpected: "INTEGER",
    DateExpected: "DATE",
    EnumExpected: "ENUM",
    RegionSetExpected: "REGION_SET",
    StringSetExpected: "STRING_SET",
    RangeExpected: "RANGE",
    BooleanExpected: "BOOLEAN",
}

_ENUM_SUBJECTS = {
    Subject.BUSINESS_ENTITY_TYPE,
    Subject.ORGANIZATION_TYPE,
    Subject.COMPANY_SCALE,
    Subject.DELINQUENCY_STATUS,
}
_ENUM_VALUES_BY_SUBJECT = {
    Subject.BUSINESS_ENTITY_TYPE: frozenset(item.value for item in BusinessEntityType),
    Subject.ORGANIZATION_TYPE: frozenset(item.value for item in OrganizationType),
    Subject.COMPANY_SCALE: frozenset(
        item.value for item in CompanyScale if item is not CompanyScale.UNKNOWN
    ),
    Subject.DELINQUENCY_STATUS: frozenset(item.value for item in DelinquencyStatus),
}
_STRING_SUBJECTS = {Subject.PRIMARY_INDUSTRY}
_STRING_COLLECTION_SUBJECTS = {
    Subject.SECONDARY_INDUSTRY,
    Subject.CERTIFICATION,
    Subject.SUPPORT_HISTORY,
    Subject.CAPABILITY_TAG,
}


def _condition_shapes() -> dict[Subject, dict[ConditionOperator, tuple[type[StrictIRModel], ...]]]:
    enum_shapes = {
        ConditionOperator.EQ: (EnumExpected,),
        ConditionOperator.NE: (EnumExpected,),
        ConditionOperator.IN: (StringSetExpected,),
        ConditionOperator.NOT_IN: (StringSetExpected,),
    }
    string_shapes = {
        ConditionOperator.EQ: (StringExpected,),
        ConditionOperator.NE: (StringExpected,),
        ConditionOperator.IN: (StringSetExpected,),
        ConditionOperator.NOT_IN: (StringSetExpected,),
        ConditionOperator.CONTAINS: (StringExpected,),
        ConditionOperator.NOT_CONTAINS: (StringExpected,),
        ConditionOperator.SEMANTIC_MATCH: (StringExpected,),
    }
    collection_shapes = {
        ConditionOperator.IN: (StringSetExpected,),
        ConditionOperator.NOT_IN: (StringSetExpected,),
        ConditionOperator.CONTAINS: (StringExpected,),
        ConditionOperator.NOT_CONTAINS: (StringExpected,),
        ConditionOperator.SEMANTIC_MATCH: (StringExpected,),
    }
    numeric_shapes = {
        ConditionOperator.EQ: (IntegerExpected,),
        ConditionOperator.NE: (IntegerExpected,),
        ConditionOperator.LT: (IntegerExpected,),
        ConditionOperator.LTE: (IntegerExpected,),
        ConditionOperator.GT: (IntegerExpected,),
        ConditionOperator.GTE: (IntegerExpected,),
        ConditionOperator.BETWEEN: (RangeExpected,),
    }
    shapes: dict[Subject, dict[ConditionOperator, tuple[type[StrictIRModel], ...]]] = {
        subject: dict(enum_shapes) for subject in _ENUM_SUBJECTS
    }
    shapes.update({subject: dict(string_shapes) for subject in _STRING_SUBJECTS})
    shapes.update({subject: dict(collection_shapes) for subject in _STRING_COLLECTION_SUBJECTS})
    shapes[Subject.FOUNDED_ON] = {
        operator: (DateExpected,)
        for operator in (
            ConditionOperator.EQ,
            ConditionOperator.NE,
            ConditionOperator.LT,
            ConditionOperator.LTE,
            ConditionOperator.GT,
            ConditionOperator.GTE,
        )
    }
    shapes[Subject.ELIGIBLE_REGION] = {
        ConditionOperator.IN: (RegionSetExpected,),
        ConditionOperator.NOT_IN: (RegionSetExpected,),
    }
    shapes[Subject.ANNUAL_REVENUE] = dict(numeric_shapes)
    shapes[Subject.EMPLOYEE_COUNT] = dict(numeric_shapes)
    shapes[Subject.OTHER] = {ConditionOperator.SEMANTIC_MATCH: (StringExpected, BooleanExpected)}
    return shapes


_CONDITION_SHAPES = _condition_shapes()
_SUBJECT_UNITS = {
    Subject.ANNUAL_REVENUE: "원",
    Subject.EMPLOYEE_COUNT: "명",
}


class Track(StrictIRModel):
    track_id: str
    label: str


class Role(StrictIRModel):
    role_key: str
    label: str


class ConditionGroup(StrictIRModel):
    group_id: str
    parent_group_id: str | None
    operator: GroupOperator
    track_ids: list[str]
    role_keys: list[str]


class Evidence(StrictIRModel):
    source_file_id: str
    source_version: str
    page: int | None
    verbatim_text: str
    source_priority: int


class Condition(StrictIRModel):
    condition_id: str
    group_id: str
    kind: ConditionKind
    subject: Subject
    operator: ConditionOperator
    expected_value: ExpectedValue | None
    unit: str | None
    reference_date: date | None
    evidence: list[Evidence]

    @model_validator(mode="after")
    def validate_comparison_shape(self) -> Condition:
        required_unit = _SUBJECT_UNITS.get(self.subject)
        if self.operator is ConditionOperator.EXISTS:
            if self.subject is Subject.OTHER:
                raise ValueError("OTHER conditions must use SEMANTIC_MATCH")
            if self.expected_value is not None:
                raise ValueError("EXISTS conditions must not have an expected value")
            if self.unit is not None:
                raise ValueError("EXISTS conditions must not have a unit")
        else:
            allowed_types = _CONDITION_SHAPES[self.subject].get(self.operator)
            if allowed_types is None:
                raise ValueError(
                    f"{self.subject.value} does not support operator {self.operator.value}"
                )
            nullable_other = (
                self.subject is Subject.OTHER and self.operator is ConditionOperator.SEMANTIC_MATCH
            )
            if (self.expected_value is None and not nullable_other) or (
                self.expected_value is not None
                and not isinstance(self.expected_value, allowed_types)
            ):
                allowed_names = ", ".join(_EXPECTED_TYPE_BY_MODEL[item] for item in allowed_types)
                actual_name = (
                    "null"
                    if self.expected_value is None
                    else _EXPECTED_TYPE_BY_MODEL[type(self.expected_value)]
                )
                raise ValueError(
                    f"{self.subject.value}/{self.operator.value} requires expected type "
                    f"{allowed_names}; got {actual_name}"
                )
            if required_unit is not None and self.unit != required_unit:
                raise ValueError(
                    f"{self.subject.value} requires unit {required_unit}; got {self.unit!r}"
                )
            if required_unit is None and self.unit is not None:
                raise ValueError(f"{self.subject.value} conditions must not have a unit")
            enum_values = _ENUM_VALUES_BY_SUBJECT.get(self.subject)
            if enum_values is not None:
                expected_values = (
                    self.expected_value.value
                    if isinstance(self.expected_value, StringSetExpected)
                    else [self.expected_value.value]
                )
                invalid_values = sorted(set(expected_values) - enum_values)
                if invalid_values:
                    raise ValueError(
                        f"{self.subject.value} has unsupported enum values: "
                        f"{', '.join(invalid_values)}"
                    )
        if not self.evidence:
            raise ValueError("Every public condition requires evidence")
        return self


class Question(StrictIRModel):
    question_id: str
    condition_id: str
    prompt: str
    answer_type: Literal["STRING", "INTEGER", "DATE", "BOOLEAN", "STRING_SET"]
    options: list[str] | None
    unit: str | None
    evidence: list[Evidence] = Field(min_length=1)


class CanonicalIR(StrictIRModel):
    analysis_version: str
    summary: str
    tracks: list[Track]
    roles: list[Role]
    groups: list[ConditionGroup]
    conditions: list[Condition]
    questions: list[Question]

    @model_validator(mode="after")
    def references_exist(self) -> CanonicalIR:
        track_ids = {track.track_id for track in self.tracks}
        role_keys = {role.role_key for role in self.roles}
        group_ids = {group.group_id for group in self.groups}
        condition_ids = {condition.condition_id for condition in self.conditions}
        if len(group_ids) != len(self.groups) or len(condition_ids) != len(self.conditions):
            raise ValueError("IR identifiers must be unique")
        for group in self.groups:
            if group.parent_group_id is not None and group.parent_group_id not in group_ids:
                raise ValueError(f"Unknown parent group: {group.parent_group_id}")
            if not set(group.track_ids) <= track_ids or not set(group.role_keys) <= role_keys:
                raise ValueError(f"Unknown track or role in group: {group.group_id}")
        if any(condition.group_id not in group_ids for condition in self.conditions):
            raise ValueError("Condition references an unknown group")
        if any(question.condition_id not in condition_ids for question in self.questions):
            raise ValueError("Question references an unknown condition")
        return self


@dataclass(frozen=True)
class EvidenceSource:
    source_file_id: str
    source_version: str
    text: str
    pages: dict[int, str] = field(default_factory=dict)
    page_capable: bool = False


class EvidenceValidationError(ValueError):
    pass


def validate_evidence(ir: CanonicalIR, sources: list[EvidenceSource]) -> None:
    """Require every verbatim quote to occur in its stored source/location."""

    source_map = {(source.source_file_id, source.source_version): source for source in sources}
    evidence_owners = [
        (f"condition {condition.condition_id}", condition.evidence) for condition in ir.conditions
    ] + [(f"question {question.question_id}", question.evidence) for question in ir.questions]
    for owner, evidence_items in evidence_owners:
        for evidence in evidence_items:
            source = source_map.get((evidence.source_file_id, evidence.source_version))
            if source is None:
                raise EvidenceValidationError(f"Unknown evidence source for {owner}")
            if (source.page_capable or source.pages) and evidence.page is None:
                raise EvidenceValidationError(f"Paged evidence requires a page for {owner}")
            haystack = (
                source.pages.get(evidence.page, "") if evidence.page is not None else source.text
            )
            if not evidence.verbatim_text.strip() or evidence.verbatim_text not in haystack:
                raise EvidenceValidationError(f"Evidence does not occur verbatim for {owner}")
