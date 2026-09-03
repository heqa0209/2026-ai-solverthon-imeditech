from __future__ import annotations

from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthContext, current_auth, require_csrf
from app.db import get_db
from app.errors import ApiError
from app.jobs import enqueue_job
from app.models import Announcement, AnnouncementVersion, CompanyProfile, CompanyProfileVersion
from app.regions import search_regions, validate_regions
from app.schemas import (
    CompanyProfileInput,
    CompanyProfileView,
    CompanyResponse,
    CompanyVersionItem,
    CompanyVersionsResponse,
    RegionSearchResponse,
)

router = APIRouter(prefix="/api/v1", tags=["company"])


def _etag(version: int) -> str:
    return f'"{version}"'


def _parse_if_match(value: str | None) -> int:
    if value is None:
        raise ApiError(428, "IF_MATCH_REQUIRED", "If-Match 헤더가 필요합니다.")
    if len(value) < 3 or not value.startswith('"') or not value.endswith('"'):
        raise ApiError(422, "IF_MATCH_INVALID", "If-Match는 따옴표로 감싼 버전이어야 합니다.")
    try:
        return int(value[1:-1])
    except ValueError as exc:
        raise ApiError(422, "IF_MATCH_INVALID", "If-Match 버전이 올바르지 않습니다.") from exc


def _view(profile: CompanyProfile, version: CompanyProfileVersion) -> CompanyProfileView:
    payload = CompanyProfileInput.model_validate(version.snapshot).model_dump(mode="python")
    return CompanyProfileView(
        **payload,
        id=profile.id,
        version=version.version,
        createdAt=version.created_at,
        updatedAt=version.created_at,
    )


def _should_auto_reevaluate(version: AnnouncementVersion) -> bool:
    if version.recruitment_starts_on is None and version.recruitment_ends_on is None:
        return True
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    if version.recruitment_starts_on and today < version.recruitment_starts_on:
        return False
    return not version.recruitment_ends_on or today <= version.recruitment_ends_on


async def _current_profile(
    db: AsyncSession, user_id: str
) -> tuple[CompanyProfile, CompanyProfileVersion] | None:
    profile = await db.scalar(select(CompanyProfile).where(CompanyProfile.user_id == user_id))
    if profile is None or profile.current_version_id is None:
        return None
    version = await db.get(CompanyProfileVersion, profile.current_version_id)
    if version is None:
        raise ApiError(500, "PROFILE_POINTER_INVALID", "기업정보를 불러올 수 없습니다.")
    return profile, version


@router.get("/company", response_model=CompanyResponse)
async def get_company(
    response: Response,
    auth: Annotated[AuthContext, Depends(current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CompanyResponse:
    current = await _current_profile(db, auth.user.id)
    if current is None:
        response.headers["ETag"] = _etag(0)
        return CompanyResponse(profile=None, version=0)
    profile, version = current
    response.headers["ETag"] = _etag(version.version)
    return CompanyResponse(profile=_view(profile, version), version=version.version)


@router.put("/company", response_model=CompanyResponse)
async def put_company(
    body: CompanyProfileInput,
    request: Request,
    response: Response,
    auth: Annotated[AuthContext, Depends(require_csrf)],
    db: Annotated[AsyncSession, Depends(get_db)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> CompanyResponse:
    expected_version = _parse_if_match(if_match)
    try:
        validate_regions(body.eligibleRegions)
    except ValueError as exc:
        raise ApiError(
            422,
            "REGION_INVALID",
            "선택한 지역을 확인해 주세요.",
            [{"location": ["body", "eligibleRegions"], "reason": str(exc)}],
        ) from exc

    profile = await db.scalar(
        select(CompanyProfile).where(CompanyProfile.user_id == auth.user.id).with_for_update()
    )
    current_version = 0
    if profile and profile.current_version_id:
        version = await db.get(CompanyProfileVersion, profile.current_version_id)
        current_version = version.version if version else 0
    if expected_version != current_version:
        raise ApiError(409, "COMPANY_VERSION_CONFLICT", "기업정보가 다른 곳에서 변경되었습니다.")
    if profile is None:
        profile = CompanyProfile(user_id=auth.user.id)
        db.add(profile)
        await db.flush()

    snapshot = body.model_dump(mode="json")
    raw_input = await request.json()
    next_version = CompanyProfileVersion(
        profile_id=profile.id,
        user_id=auth.user.id,
        version=current_version + 1,
        snapshot=snapshot,
        raw_input=raw_input,
    )
    db.add(next_version)
    await db.flush()
    profile.current_version_id = next_version.id

    announcement_rows = (
        await db.execute(
            select(Announcement, AnnouncementVersion)
            .join(AnnouncementVersion, AnnouncementVersion.id == Announcement.current_version_id)
            .where(Announcement.source_available.is_(True))
        )
    ).all()
    for announcement, announcement_version in announcement_rows:
        if not _should_auto_reevaluate(announcement_version):
            continue
        await enqueue_job(
            db,
            "DECISION_REEVALUATE",
            {
                "userId": auth.user.id,
                "announcementId": announcement.id,
                "announcementVersionId": announcement.current_version_id,
                "companyProfileVersionId": next_version.id,
                "cause": "COMPANY_PROFILE_CHANGED",
            },
        )
    await db.commit()
    await db.refresh(profile)
    await db.refresh(next_version)
    response.headers["ETag"] = _etag(next_version.version)
    return CompanyResponse(profile=_view(profile, next_version), version=next_version.version)


@router.get("/company/versions", response_model=CompanyVersionsResponse)
async def company_versions(
    auth: Annotated[AuthContext, Depends(current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CompanyVersionsResponse:
    versions = list(
        (
            await db.scalars(
                select(CompanyProfileVersion)
                .where(CompanyProfileVersion.user_id == auth.user.id)
                .order_by(CompanyProfileVersion.version.desc())
            )
        ).all()
    )
    return CompanyVersionsResponse(
        items=[
            CompanyVersionItem(
                id=version.id,
                version=version.version,
                profile=CompanyProfileInput.model_validate(version.snapshot),
                createdAt=version.created_at,
            )
            for version in versions
        ]
    )


@router.get("/regions", response_model=RegionSearchResponse)
async def regions(
    query: Annotated[str, Query(min_length=1, max_length=100)],
    _: Annotated[AuthContext, Depends(current_auth)],
) -> RegionSearchResponse:
    return RegionSearchResponse(items=search_regions(query))
