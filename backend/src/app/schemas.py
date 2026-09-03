from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal
from unicodedata import normalize

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.enums import (
    ConditionStatus,
    DecisionFreshness,
    DownloadStatus,
    ExtractionStatus,
    InterestStatus,
    Verdict,
)

Trimmed = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BusinessEntityType(StrEnum):
    SOLE_PROPRIETOR = "SOLE_PROPRIETOR"
    CORPORATION = "CORPORATION"


class OrganizationType(StrEnum):
    FOR_PROFIT = "FOR_PROFIT"
    NON_PROFIT = "NON_PROFIT"
    COOPERATIVE = "COOPERATIVE"
    PRODUCER_ORGANIZATION = "PRODUCER_ORGANIZATION"


class CompanyScale(StrEnum):
    MICRO = "MICRO"
    SMALL = "SMALL"
    MEDIUM = "MEDIUM"
    MID_SIZED = "MID_SIZED"
    LARGE = "LARGE"
    UNKNOWN = "UNKNOWN"


class DelinquencyStatus(StrEnum):
    NONE = "NONE"
    PRESENT = "PRESENT"


class RegionInput(StrictModel):
    code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=10)]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class SupportHistoryInput(StrictModel):
    programName: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]
    year: int = Field(ge=1900)

    @field_validator("year")
    @classmethod
    def not_future(cls, value: int) -> int:
        if value > date.today().year:
            raise ValueError("year cannot be in the future")
        return value


class CompanyProfileInput(StrictModel):
    companyName: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]
    businessEntityType: BusinessEntityType | None = None
    organizationType: OrganizationType | None = None
    companyScale: CompanyScale | None = None
    foundedOn: date | None = None
    eligibleRegions: list[RegionInput] = Field(default_factory=list, max_length=50)
    primaryIndustry: Annotated[
        str | None, StringConstraints(strip_whitespace=True, max_length=100)
    ] = None
    secondaryIndustries: list[Trimmed] = Field(default_factory=list, max_length=50)
    annualRevenue: int | None = Field(default=None, ge=0, le=100_000_000_000_000_000)
    employeeCount: int | None = Field(default=None, ge=0, le=10_000_000)
    delinquencyStatus: DelinquencyStatus | None = None
    certifications: list[Trimmed] = Field(default_factory=list, max_length=50)
    supportHistory: list[SupportHistoryInput] = Field(default_factory=list, max_length=100)
    capabilityTags: list[Trimmed] = Field(default_factory=list, max_length=50)
    interestKeywords: list[Trimmed] = Field(default_factory=list, max_length=50)
    excludedKeywords: list[Trimmed] = Field(default_factory=list, max_length=50)

    @field_validator("foundedOn")
    @classmethod
    def founded_not_future(cls, value: date | None) -> date | None:
        if value and value > date.today():
            raise ValueError("foundedOn cannot be in the future")
        return value

    @model_validator(mode="after")
    def normalize_lists(self) -> CompanyProfileInput:
        for name in (
            "secondaryIndustries",
            "certifications",
            "capabilityTags",
            "interestKeywords",
            "excludedKeywords",
        ):
            values = getattr(self, name)
            seen: set[str] = set()
            normalized: list[str] = []
            for value in values:
                folded = normalize("NFKC", " ".join(value.split())).casefold()
                if folded and folded not in seen:
                    seen.add(folded)
                    normalized.append(" ".join(value.split()))
            setattr(self, name, normalized)
        return self

    @field_validator("primaryIndustry")
    @classmethod
    def blank_primary_industry_is_null(cls, value: str | None) -> str | None:
        return value or None

    @model_validator(mode="after")
    def normalize_regions_and_history(self) -> CompanyProfileInput:
        region_seen: set[str] = set()
        regions: list[RegionInput] = []
        for region in self.eligibleRegions:
            if region.code not in region_seen:
                region_seen.add(region.code)
                regions.append(region)
        self.eligibleRegions = regions

        history_seen: set[tuple[str, int]] = set()
        history: list[SupportHistoryInput] = []
        for item in self.supportHistory:
            key = (normalize("NFKC", " ".join(item.programName.split())).casefold(), item.year)
            if key not in history_seen:
                history_seen.add(key)
                history.append(item)
        self.supportHistory = history
        return self


class UserView(StrictModel):
    id: str
    username: str


class AuthResponse(StrictModel):
    user: UserView


class CsrfResponse(StrictModel):
    csrfToken: str


class CompanyProfileView(CompanyProfileInput):
    id: str
    version: int
    createdAt: datetime
    updatedAt: datetime


class CompanyResponse(StrictModel):
    profile: CompanyProfileView | None
    version: int


class QueuedResponse(StrictModel):
    requestId: str
    status: Literal["QUEUED"] = "QUEUED"


class InterestInput(StrictModel):
    status: InterestStatus


class InterestResponse(StrictModel):
    status: InterestStatus
    updatedAt: datetime


class AnnouncementListItem(StrictModel):
    id: str
    announcementVersionId: str
    companyProfileVersionId: str | None
    title: str
    agencyName: str | None
    recruitmentStartsOn: date | None
    recruitmentEndsOn: date | None
    recruitmentStatus: Literal["OPEN", "CLOSED", "UNKNOWN"]
    eligibility: Verdict
    reason: str
    interestStatus: InterestStatus | None
    decisionFreshness: DecisionFreshness


class AnnouncementPage(StrictModel):
    items: list[AnnouncementListItem]
    page: int
    pageSize: Literal[10] = 10
    total: int


class HealthResponse(StrictModel):
    status: Literal["ok"] = "ok"


class ReadinessCheck(StrictModel):
    status: Literal["ok", "error"]
    detail: str | None = None


class ReadinessResponse(StrictModel):
    status: Literal["ok", "error"]
    checks: dict[str, ReadinessCheck]


class LoginInput(StrictModel):
    username: str
    password: str = Field(min_length=1, max_length=1024)


class CompanyVersionItem(StrictModel):
    id: str
    version: int
    profile: CompanyProfileInput
    createdAt: datetime


class CompanyVersionsResponse(StrictModel):
    items: list[CompanyVersionItem]


class RegionItem(StrictModel):
    code: str
    name: str
    parentCode: str | None
    parentName: str | None
    level: Literal["SIDO", "SIGUNGU"]


class RegionSearchResponse(StrictModel):
    items: list[RegionItem]


class RoleInput(StrictModel):
    announcementVersionId: str
    roleKey: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=100)]


class AnswerInput(StrictModel):
    announcementVersionId: str
    conditionId: str
    value: str | int | bool | list[str] | dict[str, object]
    source: Literal["USER_VERIFIED", "OFFICIAL_DOCUMENT", "AGENCY_INQUIRY"]
    memo: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=1000)] = None


class ReevaluateInput(StrictModel):
    announcementVersionId: str


class EvidenceView(StrictModel):
    sourceFileId: str | None = None
    sourceVersion: str | None = None
    sourceName: str | None = None
    page: int | None = None
    verbatimText: str
    sourcePriority: int | None = None


class ConditionResultView(StrictModel):
    id: str
    conditionKey: str
    groupKey: str
    trackKey: str | None
    roleKey: str | None
    kind: str
    subject: str
    operator: str
    expectedValue: dict[str, object] | None
    unit: str | None
    referenceDate: date | None
    status: ConditionStatus
    usedValue: dict[str, object] | None
    explanation: str | None
    assumptionCode: str | None
    evidence: list[EvidenceView]


class QuestionView(StrictModel):
    conditionId: str
    prompt: str
    valueType: str
    options: list[str] | None = None
    unit: str | None = None
    evidence: list[EvidenceView] = Field(default_factory=list)


class SourceFileView(StrictModel):
    id: str
    name: str
    sourceUrl: str
    sizeBytes: int | None
    mimeType: str | None
    sourceOrder: int
    downloadStatus: DownloadStatus
    extractionStatus: ExtractionStatus
    failureCode: str | None


class RolePredictionView(StrictModel):
    roleKey: str
    label: str
    eligibility: Verdict | None = None


class AnnouncementDetail(AnnouncementListItem):
    sourceUrl: str
    publishedOn: date | None
    summary: str | None
    explanation: str | None
    passedTrackKey: str | None
    selectedRoleKey: str | None
    rolePredictions: list[RolePredictionView]
    conditions: list[ConditionResultView]
    questions: list[QuestionView]
    files: list[SourceFileView]


class ErrorDetail(StrictModel):
    location: list[str | int]
    reason: str


class ErrorResponse(StrictModel):
    code: str
    message: str
    details: list[ErrorDetail]
    requestId: str
