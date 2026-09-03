from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Session(Base, TimestampMixin):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(50), index=True)
    ip_address: Mapped[str] = mapped_column(String(64), index=True)
    succeeded: Mapped[bool] = mapped_column(Boolean, default=False)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, index=True
    )


class CompanyProfile(Base, TimestampMixin):
    __tablename__ = "company_profiles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    current_version_id: Mapped[str | None] = mapped_column(String(36), unique=True)


class CompanyProfileVersion(Base):
    __tablename__ = "company_profile_versions"
    __table_args__ = (UniqueConstraint("profile_id", "version", name="uq_profile_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    profile_id: Mapped[str] = mapped_column(ForeignKey("company_profiles.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    raw_input: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Announcement(Base, TimestampMixin):
    __tablename__ = "announcements"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    source_url: Mapped[str] = mapped_column(Text)
    current_version_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    source_available: Mapped[bool] = mapped_column(Boolean, default=True)


class AnnouncementVersion(Base):
    __tablename__ = "announcement_versions"
    __table_args__ = (
        UniqueConstraint("announcement_id", "content_hash", name="uq_announcement_hash"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    announcement_id: Mapped[str] = mapped_column(ForeignKey("announcements.id"), index=True)
    raw_payload: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(Text)
    agency_name: Mapped[str | None] = mapped_column(Text)
    summary_text: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    published_on: Mapped[date | None] = mapped_column(Date)
    recruitment_starts_on: Mapped[date | None] = mapped_column(Date)
    recruitment_ends_on: Mapped[date | None] = mapped_column(Date, index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class SourceFile(Base):
    __tablename__ = "source_files"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    announcement_version_id: Mapped[str] = mapped_column(
        ForeignKey("announcement_versions.id"), index=True
    )
    name: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    storage_path: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    source_order: Mapped[int] = mapped_column(Integer, default=0)
    source_priority: Mapped[int] = mapped_column(Integer, default=0)
    download_status: Mapped[str] = mapped_column(String(32), default="PENDING")
    extraction_status: Mapped[str] = mapped_column(String(32), default="PENDING")
    failure_code: Mapped[str | None] = mapped_column(String(100))
    extracted_text: Mapped[str | None] = mapped_column(Text)


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    announcement_version_id: Mapped[str] = mapped_column(
        ForeignKey("announcement_versions.id"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), index=True)
    analysis_version: Mapped[str] = mapped_column(String(64))
    canonical_ir: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))


class AIStageRun(Base):
    __tablename__ = "ai_stage_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_run_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.id"), index=True)
    stage: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    effort: Mapped[str] = mapped_column(String(16))
    prompt_version: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    structured_output: Mapped[dict | None] = mapped_column(JSON)
    evidence: Mapped[list | None] = mapped_column(JSON)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    error_code: Mapped[str | None] = mapped_column(String(100))


class ExtractedCondition(Base):
    __tablename__ = "extracted_conditions"
    __table_args__ = (
        UniqueConstraint("analysis_run_id", "condition_key", name="uq_analysis_condition"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_run_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.id"), index=True)
    condition_key: Mapped[str] = mapped_column(String(100))
    group_key: Mapped[str] = mapped_column(String(100), index=True)
    track_key: Mapped[str | None] = mapped_column(String(100))
    role_key: Mapped[str | None] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(String(64))
    operator: Mapped[str] = mapped_column(String(32))
    expected_value: Mapped[dict | None] = mapped_column(JSON)
    unit: Mapped[str | None] = mapped_column(String(32))
    reference_date: Mapped[date | None] = mapped_column(Date)
    evidence: Mapped[list] = mapped_column(JSON, default=list)


class EligibilityDecision(Base):
    __tablename__ = "eligibility_decisions"
    __table_args__ = (
        Index(
            "ix_decision_inputs", "user_id", "announcement_version_id", "company_profile_version_id"
        ),
        Index(
            "uq_current_decision_user_announcement",
            "user_id",
            "announcement_id",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current = 1"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    announcement_id: Mapped[str] = mapped_column(ForeignKey("announcements.id"), index=True)
    announcement_version_id: Mapped[str] = mapped_column(ForeignKey("announcement_versions.id"))
    company_profile_version_id: Mapped[str] = mapped_column(
        ForeignKey("company_profile_versions.id")
    )
    selected_role_key: Mapped[str | None] = mapped_column(String(100))
    calculated_verdict: Mapped[str | None] = mapped_column(String(32))
    published_verdict: Mapped[str] = mapped_column(String(32), index=True)
    decision_origin: Mapped[str] = mapped_column(String(32), default="CALCULATED")
    explanation: Mapped[str | None] = mapped_column(Text)
    passed_track_key: Mapped[str | None] = mapped_column(String(100))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class ConditionResult(Base):
    __tablename__ = "condition_results"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    decision_id: Mapped[str] = mapped_column(ForeignKey("eligibility_decisions.id"), index=True)
    condition_id: Mapped[str] = mapped_column(ForeignKey("extracted_conditions.id"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    used_value: Mapped[dict | None] = mapped_column(JSON)
    explanation: Mapped[str | None] = mapped_column(Text)
    assumption_code: Mapped[str | None] = mapped_column(String(100))
    evidence: Mapped[list] = mapped_column(JSON, default=list)


class AnnouncementInterest(Base, TimestampMixin):
    __tablename__ = "announcement_interests"
    __table_args__ = (UniqueConstraint("user_id", "announcement_id", name="uq_user_interest"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    announcement_id: Mapped[str] = mapped_column(ForeignKey("announcements.id"), index=True)
    status: Mapped[str] = mapped_column(String(32))


class AnnouncementRoleSelection(Base):
    __tablename__ = "announcement_role_selections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    announcement_id: Mapped[str] = mapped_column(ForeignKey("announcements.id"), index=True)
    announcement_version_id: Mapped[str] = mapped_column(ForeignKey("announcement_versions.id"))
    role_key: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class AnnouncementAnswer(Base):
    __tablename__ = "announcement_answers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    announcement_version_id: Mapped[str] = mapped_column(
        ForeignKey("announcement_versions.id"), index=True
    )
    condition_id: Mapped[str] = mapped_column(ForeignKey("extracted_conditions.id"), index=True)
    value: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(32))
    memo: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="QUEUED", index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, index=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    isolation_ok: Mapped[bool] = mapped_column(Boolean, default=False)
