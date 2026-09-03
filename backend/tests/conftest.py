from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.auth import create_user
from app.config import Settings, get_settings
from app.db import get_db
from app.health import get_codex_login_status
from app.main import create_app
from app.models import Base


@pytest.fixture
def session_factory(tmp_path: Path):
    database = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}", poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def setup() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as db:
            await create_user(db, "demo.user", "correct horse battery staple")

    asyncio.run(setup())
    yield factory
    asyncio.run(engine.dispose())


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    storage = tmp_path / "sources"
    storage.mkdir()
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'unused.db'}",
        app_origin="http://testserver",
        session_secret="test-session-secret-that-is-at-least-32-bytes",
        source_storage_root=storage,
        demo_fixture_root=tmp_path,
        bizinfo_api_key="test-key",
        session_cookie_secure=False,
    )


@pytest.fixture
def client(session_factory, settings: Settings) -> TestClient:
    async def override_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as db:
            yield db

    app = create_app()
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_codex_login_status] = lambda: True
    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


@pytest.fixture
def authenticated_client(client: TestClient) -> tuple[TestClient, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "  DEMO.USER ", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    csrf = client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    return client, csrf.json()["csrfToken"]
