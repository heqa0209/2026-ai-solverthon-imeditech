from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    value: str


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
    value: list[str]


class StringSetExpected(StrictIRModel):
    type: Literal["STRING_SET"]
    value: list[str]


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


class Track(StrictIRModel):
    track_id: str
    label: str


class Role(StrictIRModel):
    role_id: str
    label: str


class ConditionGroup(StrictIRModel):
    group_id: str
    parent_group_id: str | None
    operator: GroupOperator
    track_ids: list[str]
    role_ids: list[str]


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
    def other_is_not_automatically_comparable(self) -> Condition:
        if self.subject is Subject.OTHER and self.operator is not ConditionOperator.SEMANTIC_MATCH:
            raise ValueError("OTHER conditions must use SEMANTIC_MATCH")
        if not self.evidence:
            raise ValueError("Every public condition requires evidence")
        return self


class Question(StrictIRModel):
    question_id: str
    condition_id: str
    prompt: str
    answer_type: Literal["STRING", "INTEGER", "DATE", "BOOLEAN", "STRING_SET"]


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
        role_ids = {role.role_id for role in self.roles}
        group_ids = {group.group_id for group in self.groups}
        condition_ids = {condition.condition_id for condition in self.conditions}
        if len(group_ids) != len(self.groups) or len(condition_ids) != len(self.conditions):
            raise ValueError("IR identifiers must be unique")
        for group in self.groups:
            if group.parent_group_id is not None and group.parent_group_id not in group_ids:
                raise ValueError(f"Unknown parent group: {group.parent_group_id}")
            if not set(group.track_ids) <= track_ids or not set(group.role_ids) <= role_ids:
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


class EvidenceValidationError(ValueError):
    pass


def validate_evidence(ir: CanonicalIR, sources: list[EvidenceSource]) -> None:
    """Require every verbatim quote to occur in its stored source/location."""

    source_map = {(source.source_file_id, source.source_version): source for source in sources}
    for condition in ir.conditions:
        for evidence in condition.evidence:
            source = source_map.get((evidence.source_file_id, evidence.source_version))
            if source is None:
                raise EvidenceValidationError(
                    f"Unknown evidence source for condition {condition.condition_id}"
                )
            haystack = (
                source.pages.get(evidence.page, "") if evidence.page is not None else source.text
            )
            if not evidence.verbatim_text.strip() or evidence.verbatim_text not in haystack:
                raise EvidenceValidationError(
                    f"Evidence does not occur verbatim for condition {condition.condition_id}"
                )
