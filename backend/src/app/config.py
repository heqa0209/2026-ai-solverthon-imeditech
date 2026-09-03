from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AnyHttpUrl, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://solverthon:solverthon@127.0.0.1:5432/solverthon"
    app_origin: AnyHttpUrl = "http://localhost:5173"
    session_secret: str = Field(default="test-session-secret-change-me-32-bytes", min_length=32)
    bizinfo_api_key: str | None = None
    source_storage_root: Path = REPO_ROOT / "storage/sources"
    demo_fixture_root: Path = REPO_ROOT / "fixtures/demo"
    app_timezone: str = "Asia/Seoul"
    ai_max_concurrency: int = Field(default=5, ge=1, le=5)
    ai_stage_timeout_seconds: int = Field(default=300, ge=1, le=900)
    session_cookie_secure: bool = False

    @field_validator("app_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        if value != "Asia/Seoul":
            raise ValueError("APP_TIMEZONE must be Asia/Seoul")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
