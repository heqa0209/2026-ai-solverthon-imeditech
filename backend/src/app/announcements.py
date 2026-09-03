from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthContext, current_auth, require_csrf
from app.company import _current_profile
from app.config import Settings, get_settings
from app.db import get_db
from app.decision_lock import serialize_decision_state
from app.domain.eligibility import evaluate_decision
from app.enums import ConditionStatus, DecisionFreshness, Verdict
from app.errors import ApiError
from app.jobs import enqueue_job
from app.models import (
    AnalysisRun,
    Announcement,
    AnnouncementAnswer,
    AnnouncementInterest,
    AnnouncementRoleSelection,
    AnnouncementVersion,
    ConditionResult,
    EligibilityDecision,
    ExtractedCondition,
    SourceFile,
)
from app.schemas import (
    AnnouncementDetail,
    AnnouncementListItem,
    AnnouncementPage,
    AnswerInput,
    ConditionResultView,
    EvidenceView,
    InterestInput,
    InterestResponse,
    QuestionView,
    QueuedResponse,
    ReevaluateInput,
    RoleInput,
    RolePredictionView,
    SourceFileView,
)

router = APIRouter(prefix="/api/v1/announcements", tags=["announcements"])


def _today() -> date:
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


def recruitment_status(version: AnnouncementVersion) -> str:
    today = _today()
    if version.recruitment_starts_on is None and version.recruitment_ends_on is None:
        return "UNKNOWN"
    if version.recruitment_starts_on and today < version.recruitment_starts_on:
        return "CLOSED"
    if version.recruitment_ends_on and today > version.recruitment_ends_on:
        return "CLOSED"
    return "OPEN"


def decision_freshness(
    announcement: Announcement,
    decision: EligibilityDecision,
    company_profile_version_id: str | None,
) -> DecisionFreshness:
    if decision.announcement_version_id != announcement.current_version_id:
        return DecisionFreshness.ANNOUNCEMENT_CHANGED
    if decision.company_profile_version_id != company_profile_version_id:
        return DecisionFreshness.COMPANY_PROFILE_CHANGED
    return DecisionFreshness.CURRENT


def _list_item(
    announcement: Announcement,
    version: AnnouncementVersion,
    decision: EligibilityDecision,
    interest: AnnouncementInterest | None,
    profile_version_id: str | None,
) -> AnnouncementListItem:
    return AnnouncementListItem(
        id=announcement.id,
        announcementVersionId=version.id,
        companyProfileVersionId=decision.company_profile_version_id,
        decisionId=decision.id,
        decisionPublishedAt=decision.created_at,
        title=version.title,
        agencyName=version.agency_name,
        recruitmentStartsOn=version.recruitment_starts_on,
        recruitmentEndsOn=version.recruitment_ends_on,
        recruitmentStatus=recruitment_status(version),
        eligibility=Verdict(decision.published_verdict),
        reason=decision.explanation or "원문 조건을 확인해 주세요.",
        interestStatus=interest.status if interest else None,
        decisionFreshness=decision_freshness(announcement, decision, profile_version_id),
    )


async def _visible_rows(db: AsyncSession, user_id: str):
    interest_join = and_(
        AnnouncementInterest.user_id == user_id,
        AnnouncementInterest.announcement_id == Announcement.id,
    )
    statement = (
        select(
            Announcement,
            AnnouncementVersion,
            EligibilityDecision,
            AnnouncementInterest,
        )
        .join(AnnouncementVersion, AnnouncementVersion.id == Announcement.current_version_id)
        .join(
            EligibilityDecision,
            and_(
                EligibilityDecision.announcement_id == Announcement.id,
                EligibilityDecision.user_id == user_id,
                EligibilityDecision.is_current.is_(True),
            ),
        )
        .outerjoin(AnnouncementInterest, interest_join)
        .where(Announcement.source_available.is_(True))
    )
    return (await db.execute(statement)).all()


async def _visible_announcement(
    db: AsyncSession, user_id: str, announcement_id: str
) -> tuple[Announcement, AnnouncementVersion, EligibilityDecision, AnnouncementInterest | None]:
    rows = await _visible_rows(db, user_id)
    for row in rows:
        if row[0].id == announcement_id:
            return row
    raise ApiError(404, "ANNOUNCEMENT_NOT_FOUND", "공고를 찾을 수 없습니다.")


@router.get("", response_model=AnnouncementPage)
async def list_announcements(
    auth: Annotated[AuthContext, Depends(current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    keyword: Annotated[str | None, Query(max_length=100)] = None,
    eligibility: Verdict | None = None,
    recruitmentStatus: Annotated[str | None, Query(pattern="^(OPEN|CLOSED|UNKNOWN)$")] = None,
    interestStatus: Annotated[
        str | None, Query(pattern="^(INTERESTED|ON_HOLD|NOT_INTERESTED|UNSET|ANY_SET)$")
    ] = None,
) -> AnnouncementPage:
    current_profile = await _current_profile(db, auth.user.id)
    profile_version_id = current_profile[1].id if current_profile else None
    rows_by_id = await _visible_rows(db, auth.user.id)
    items = [_list_item(*row, profile_version_id) for row in rows_by_id]
    if keyword:
        needle = keyword.strip().casefold()
        condition_text: dict[str, list[str]] = {}
        decision_ids = [row[2].id for row in rows_by_id]
        if decision_ids:
            condition_rows = (
                await db.execute(
                    select(ConditionResult.decision_id, ExtractedCondition)
                    .join(
                        ExtractedCondition,
                        ExtractedCondition.id == ConditionResult.condition_id,
                    )
                    .where(ConditionResult.decision_id.in_(decision_ids))
                )
            ).all()
            for decision_id, condition in condition_rows:
                condition_text.setdefault(decision_id, []).extend(
                    [
                        condition.subject,
                        json.dumps(condition.expected_value, ensure_ascii=False),
                        " ".join(
                            str(item.get("verbatim_text", ""))
                            for item in condition.evidence
                            if isinstance(item, dict)
                        ),
                    ]
                )
        source_by_id = {row[0].id: row for row in rows_by_id}
        items = [
            item
            for item in items
            if needle
            in " ".join(
                [
                    item.title,
                    item.agencyName or "",
                    item.reason,
                    source_by_id[item.id][1].summary_text or "",
                    *condition_text.get(source_by_id[item.id][2].id, []),
                ]
            ).casefold()
        ]
    if eligibility:
        items = [item for item in items if item.eligibility == eligibility]
    if recruitmentStatus:
        items = [item for item in items if item.recruitmentStatus == recruitmentStatus]
    if interestStatus:
        items = [
            item
            for item in items
            if (interestStatus == "UNSET" and item.interestStatus is None)
            or (interestStatus == "ANY_SET" and item.interestStatus is not None)
            or item.interestStatus == interestStatus
        ]

    def sort_key(item: AnnouncementListItem) -> tuple[bool, date, int, str]:
        end = item.recruitmentEndsOn
        source_row = next(row for row in rows_by_id if row[0].id == item.id)
        published = source_row[1].published_on
        return (end is None, end or date.max, -(published.toordinal() if published else 0), item.id)

    items.sort(key=sort_key)
    total = len(items)
    start = (page - 1) * 10
    return AnnouncementPage(items=items[start : start + 10], page=page, total=total)


def _evidence(raw: list[dict[str, Any]] | None) -> list[EvidenceView]:
    result: list[EvidenceView] = []
    for item in raw or []:
        text = item.get("verbatim_text") or item.get("verbatimText")
        if not text:
            continue
        result.append(
            EvidenceView(
                sourceFileId=item.get("source_file_id") or item.get("sourceFileId"),
                sourceVersion=item.get("source_version") or item.get("sourceVersion"),
                sourceName=item.get("source_name") or item.get("sourceName"),
                page=item.get("page"),
                verbatimText=str(text),
                sourcePriority=item.get("source_priority") or item.get("sourcePriority"),
            )
        )
    return result


def _safe_source_path(root_value: Path, path_value: str) -> Path | None:
    root = root_value.resolve()
    candidate = Path(path_value)
    path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


@router.get("/{announcement_id}", response_model=AnnouncementDetail)
async def announcement_detail(
    announcement_id: str,
    auth: Annotated[AuthContext, Depends(current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AnnouncementDetail:
    announcement, version, decision, interest = await _visible_announcement(
        db, auth.user.id, announcement_id
    )
    current_profile = await _current_profile(db, auth.user.id)
    profile_version_id = current_profile[1].id if current_profile else None
    item = _list_item(announcement, version, decision, interest, profile_version_id)

    condition_rows = (
        await db.execute(
            select(ExtractedCondition, ConditionResult)
            .join(ConditionResult, ConditionResult.condition_id == ExtractedCondition.id)
            .where(ConditionResult.decision_id == decision.id)
            .order_by(ExtractedCondition.group_key, ExtractedCondition.condition_key)
        )
    ).all()
    conditions = [
        ConditionResultView(
            id=condition.id,
            conditionKey=condition.condition_key,
            groupKey=condition.group_key,
            trackKey=condition.track_key,
            roleKey=condition.role_key,
            kind=condition.kind,
            subject=condition.subject,
            operator=condition.operator,
            expectedValue=condition.expected_value,
            unit=condition.unit,
            referenceDate=condition.reference_date,
            status=ConditionStatus(result.status),
            usedValue=result.used_value,
            explanation=result.explanation,
            assumptionCode=result.assumption_code,
            evidence=_evidence(result.evidence or condition.evidence),
        )
        for condition, result in condition_rows
    ]
    files = list(
        (
            await db.scalars(
                select(SourceFile)
                .where(SourceFile.announcement_version_id == version.id)
                .order_by(SourceFile.source_order, SourceFile.id)
            )
        ).all()
    )
    analysis = await db.scalar(
        select(AnalysisRun)
        .where(AnalysisRun.announcement_version_id == version.id, AnalysisRun.status == "SUCCEEDED")
        .order_by(desc(AnalysisRun.completed_at))
        .limit(1)
    )
    ir = analysis.canonical_ir if analysis and analysis.canonical_ir else {}
    role_selection = await db.scalar(
        select(AnnouncementRoleSelection)
        .where(
            AnnouncementRoleSelection.user_id == auth.user.id,
            AnnouncementRoleSelection.announcement_id == announcement.id,
            AnnouncementRoleSelection.announcement_version_id == version.id,
        )
        .order_by(desc(AnnouncementRoleSelection.created_at))
        .limit(1)
    )
    profile_snapshot = current_profile[1].snapshot if current_profile else None
    role_predictions = []
    for role in ir.get("roles", []):
        if not isinstance(role, dict) or not role.get("role_key"):
            continue
        role_key = str(role["role_key"])
        prediction = (
            evaluate_decision(ir, profile_snapshot, role_key)
            if profile_snapshot is not None
            else None
        )
        role_predictions.append(
            RolePredictionView(
                roleKey=role_key,
                label=str(role.get("label") or role_key),
                eligibility=prediction.verdict if prediction else None,
            )
        )
    condition_ids = {condition.condition_key: condition.id for condition, _ in condition_rows}
    questions = [
        QuestionView(
            conditionId=condition_ids.get(
                str(question.get("condition_id") or question.get("conditionId")),
                str(question.get("condition_id") or question.get("conditionId")),
            ),
            prompt=str(question.get("prompt") or question.get("question") or "확인이 필요합니다."),
            valueType=str(question.get("answer_type") or "STRING"),
            options=question.get("options"),
            unit=question.get("unit"),
            evidence=_evidence(question.get("evidence")),
        )
        for question in ir.get("questions", [])
        if isinstance(question, dict)
        and (question.get("condition_id") or question.get("conditionId"))
    ]
    return AnnouncementDetail(
        **item.model_dump(),
        sourceUrl=announcement.source_url,
        publishedOn=version.published_on,
        summary=version.summary_text,
        explanation=decision.explanation,
        passedTrackKey=decision.passed_track_key,
        selectedRoleKey=role_selection.role_key if role_selection else decision.selected_role_key,
        rolePredictions=role_predictions,
        conditions=conditions,
        questions=questions,
        files=[
            SourceFileView(
                id=file.id,
                name=file.name,
                sourceUrl=file.source_url,
                sizeBytes=file.size_bytes,
                mimeType=file.mime_type,
                sourceOrder=file.source_order,
                downloadStatus=file.download_status,
                extractionStatus=file.extraction_status,
                failureCode=file.failure_code,
            )
            for file in files
        ],
    )


@router.get("/{announcement_id}/files/{file_id}", response_class=FileResponse)
async def download_file(
    announcement_id: str,
    file_id: str,
    auth: Annotated[AuthContext, Depends(current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FileResponse:
    _, version, _, _ = await _visible_announcement(db, auth.user.id, announcement_id)
    source_file = await db.scalar(
        select(SourceFile).where(
            SourceFile.id == file_id,
            SourceFile.announcement_version_id == version.id,
            SourceFile.download_status == "SUCCEEDED",
        )
    )
    if source_file is None or not source_file.storage_path:
        raise ApiError(404, "FILE_NOT_FOUND", "첨부파일을 찾을 수 없습니다.")
    path = _safe_source_path(settings.source_storage_root, source_file.storage_path)
    if path is None:
        raise ApiError(404, "FILE_NOT_FOUND", "첨부파일을 찾을 수 없습니다.")
    return FileResponse(path, filename=source_file.name, media_type=source_file.mime_type)


async def _assert_current_version(
    db: AsyncSession, user_id: str, announcement_id: str, requested_version_id: str
) -> tuple[Announcement, str]:
    announcement, _, _, _ = await _visible_announcement(db, user_id, announcement_id)
    if announcement.current_version_id != requested_version_id:
        raise ApiError(409, "ANNOUNCEMENT_VERSION_CONFLICT", "공고가 변경되었습니다.")
    current_profile = await _current_profile(db, user_id)
    if current_profile is None:
        raise ApiError(409, "COMPANY_PROFILE_REQUIRED", "기업정보를 먼저 저장해 주세요.")
    return announcement, current_profile[1].id


@router.put("/{announcement_id}/interest", response_model=InterestResponse)
async def set_interest(
    announcement_id: str,
    body: InterestInput,
    auth: Annotated[AuthContext, Depends(require_csrf)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InterestResponse:
    await _visible_announcement(db, auth.user.id, announcement_id)
    interest = await db.scalar(
        select(AnnouncementInterest).where(
            AnnouncementInterest.user_id == auth.user.id,
            AnnouncementInterest.announcement_id == announcement_id,
        )
    )
    if interest is None:
        interest = AnnouncementInterest(
            user_id=auth.user.id, announcement_id=announcement_id, status=body.status
        )
        db.add(interest)
    else:
        interest.status = body.status
    await db.commit()
    await db.refresh(interest)
    return InterestResponse(status=interest.status, updatedAt=interest.updated_at)


@router.put("/{announcement_id}/role", response_model=QueuedResponse, status_code=202)
async def set_role(
    announcement_id: str,
    body: RoleInput,
    auth: Annotated[AuthContext, Depends(require_csrf)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> QueuedResponse:
    announcement, profile_version_id = await _assert_current_version(
        db, auth.user.id, announcement_id, body.announcementVersionId
    )
    await serialize_decision_state(db, user_id=auth.user.id, announcement_id=announcement_id)
    announcement, profile_version_id = await _assert_current_version(
        db, auth.user.id, announcement_id, body.announcementVersionId
    )
    analysis = await db.scalar(
        select(AnalysisRun)
        .where(
            AnalysisRun.announcement_version_id == body.announcementVersionId,
            AnalysisRun.status == "SUCCEEDED",
        )
        .order_by(desc(AnalysisRun.completed_at))
        .limit(1)
    )
    valid_roles = {
        role.get("role_key")
        for role in ((analysis.canonical_ir or {}).get("roles", []) if analysis else [])
        if isinstance(role, dict) and role.get("role_key")
    }
    if body.roleKey is not None and body.roleKey not in valid_roles:
        raise ApiError(422, "ROLE_INVALID", "선택할 수 없는 역할입니다.")
    latest = await db.scalar(
        select(AnnouncementRoleSelection)
        .where(
            AnnouncementRoleSelection.user_id == auth.user.id,
            AnnouncementRoleSelection.announcement_id == announcement_id,
            AnnouncementRoleSelection.announcement_version_id == body.announcementVersionId,
        )
        .order_by(desc(AnnouncementRoleSelection.created_at))
        .limit(1)
    )
    if latest is None or latest.role_key != body.roleKey:
        db.add(
            AnnouncementRoleSelection(
                user_id=auth.user.id,
                announcement_id=announcement.id,
                announcement_version_id=body.announcementVersionId,
                role_key=body.roleKey,
            )
        )
    job = await enqueue_job(
        db,
        "DECISION_REEVALUATE",
        {
            "userId": auth.user.id,
            "announcementId": announcement.id,
            "announcementVersionId": body.announcementVersionId,
            "companyProfileVersionId": profile_version_id,
            "selectedRoleKey": body.roleKey,
            "cause": "ROLE_SELECTED",
        },
    )
    await db.commit()
    return QueuedResponse(requestId=job.id)


def _validate_answer_value(question: dict[str, Any], value: object) -> None:
    answer_type = question.get("answer_type")
    valid = False
    if answer_type == "STRING":
        valid = type(value) is str
    elif answer_type == "INTEGER":
        valid = type(value) is int
    elif answer_type == "DATE":
        if type(value) is str:
            try:
                date.fromisoformat(value)
                valid = True
            except ValueError:
                pass
    elif answer_type == "BOOLEAN":
        valid = type(value) is bool
    elif answer_type == "STRING_SET":
        valid = isinstance(value, list) and all(type(item) is str for item in value)

    options = question.get("options")
    if valid and isinstance(options, list):
        values = value if isinstance(value, list) else [value]
        valid = all(str(item) in options for item in values)
    if not valid:
        raise ApiError(422, "ANSWER_TYPE_INVALID", "질문에 필요한 자료형으로 답변해 주세요.")


@router.post("/{announcement_id}/answers", response_model=QueuedResponse, status_code=202)
async def answer_condition(
    announcement_id: str,
    body: AnswerInput,
    auth: Annotated[AuthContext, Depends(require_csrf)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> QueuedResponse:
    announcement, profile_version_id = await _assert_current_version(
        db, auth.user.id, announcement_id, body.announcementVersionId
    )
    await serialize_decision_state(db, user_id=auth.user.id, announcement_id=announcement_id)
    announcement, profile_version_id = await _assert_current_version(
        db, auth.user.id, announcement_id, body.announcementVersionId
    )
    condition = await db.scalar(
        select(ExtractedCondition)
        .join(AnalysisRun, AnalysisRun.id == ExtractedCondition.analysis_run_id)
        .where(
            ExtractedCondition.id == body.conditionId,
            AnalysisRun.announcement_version_id == body.announcementVersionId,
        )
    )
    if condition is None:
        raise ApiError(404, "CONDITION_NOT_FOUND", "조건을 찾을 수 없습니다.")
    analysis = await db.get(AnalysisRun, condition.analysis_run_id)
    question = next(
        (
            item
            for item in ((analysis.canonical_ir or {}).get("questions", []) if analysis else [])
            if isinstance(item, dict) and item.get("condition_id") == condition.condition_key
        ),
        None,
    )
    if question is None:
        raise ApiError(422, "ANSWER_NOT_REQUESTED", "이 조건에는 저장할 질문이 없습니다.")
    _validate_answer_value(question, body.value)
    answer_payload = body.model_dump(mode="json")
    latest = await db.scalar(
        select(AnnouncementAnswer)
        .where(
            AnnouncementAnswer.user_id == auth.user.id,
            AnnouncementAnswer.announcement_version_id == body.announcementVersionId,
            AnnouncementAnswer.condition_id == body.conditionId,
        )
        .order_by(desc(AnnouncementAnswer.created_at))
        .limit(1)
    )
    if latest is None or {
        "value": latest.value,
        "source": latest.source,
        "memo": latest.memo,
    } != {"value": body.value, "source": body.source, "memo": body.memo}:
        db.add(
            AnnouncementAnswer(
                user_id=auth.user.id,
                announcement_version_id=body.announcementVersionId,
                condition_id=body.conditionId,
                value=body.value,
                source=body.source,
                memo=body.memo,
            )
        )
    job = await enqueue_job(
        db,
        "DECISION_REEVALUATE",
        {
            "userId": auth.user.id,
            "announcementId": announcement.id,
            "announcementVersionId": body.announcementVersionId,
            "companyProfileVersionId": profile_version_id,
            "answer": answer_payload,
            "cause": "ANSWER_SAVED",
        },
    )
    await db.commit()
    return QueuedResponse(requestId=job.id)


@router.post("/{announcement_id}/reevaluate", response_model=QueuedResponse, status_code=202)
async def reevaluate(
    announcement_id: str,
    body: ReevaluateInput,
    auth: Annotated[AuthContext, Depends(require_csrf)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> QueuedResponse:
    announcement, profile_version_id = await _assert_current_version(
        db, auth.user.id, announcement_id, body.announcementVersionId
    )
    job = await enqueue_job(
        db,
        "DECISION_REEVALUATE",
        {
            "userId": auth.user.id,
            "announcementId": announcement.id,
            "announcementVersionId": body.announcementVersionId,
            "companyProfileVersionId": profile_version_id,
            "cause": "USER_REQUESTED",
        },
    )
    await db.commit()
    return QueuedResponse(requestId=job.id)
