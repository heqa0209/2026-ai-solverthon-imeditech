from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.enums import DecisionFreshness, InterestStatus, Verdict

Trimmed = Annotated[str, StringConstraints(strip_whitespace=True)]


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
                folded = " ".join(value.split()).casefold()
                if folded and folded not in seen:
                    seen.add(folded)
                    normalized.append(" ".join(value.split()))
            setattr(self, name, normalized)
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
