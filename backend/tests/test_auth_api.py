from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import SESSION_COOKIE_NAME, create_user
from app.models import Session


def test_unauthenticated_response_uses_error_contract(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json().keys() == {"code", "message", "details", "requestId"}
    assert response.json()["code"] == "AUTH_REQUIRED"
    assert response.headers["X-Request-ID"] == response.json()["requestId"]


def test_login_me_csrf_and_logout(
    authenticated_client: tuple[TestClient, str], session_factory
) -> None:
    client, csrf = authenticated_client
    assert client.get("/api/v1/auth/me").json()["user"]["username"] == "demo.user"
    cookie = client.cookies.get(SESSION_COOKIE_NAME)
    assert cookie

    async def stored_token() -> str:
        async with session_factory() as db:
            return (await db.scalars(select(Session.token_hash))).one()

    assert asyncio.run(stored_token()) != cookie

    rejected = client.post(
        "/api/v1/auth/logout",
        headers={"Origin": "http://evil.example", "X-CSRF-Token": csrf},
    )
    assert rejected.status_code == 403
    response = client.post(
        "/api/v1/auth/logout",
        headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
    )
    assert response.status_code == 204
    assert response.content == b""
    assert client.get("/api/v1/auth/me").status_code == 401


def test_login_rate_limit_is_username_and_ip_scoped(client: TestClient) -> None:
    for _ in range(5):
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "demo.user", "password": "wrong"},
        )
        assert response.status_code == 401
    blocked = client.post(
        "/api/v1/auth/login",
        json={"username": "demo.user", "password": "correct horse battery staple"},
    )
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "LOGIN_RATE_LIMITED"


def test_validation_error_rejects_unknown_fields(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "demo.user", "password": "x", "token": "not-allowed"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_invalid_username_cannot_be_truncated_into_an_existing_user(
    client: TestClient, session_factory
) -> None:
    valid_username = "a" * 50

    async def seed_user() -> None:
        async with session_factory() as db:
            await create_user(db, valid_username, "known-password")

    asyncio.run(seed_user())
    response = client.post(
        "/api/v1/auth/login",
        json={"username": f"{valid_username}!", "password": "known-password"},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "LOGIN_FAILED"
    assert SESSION_COOKIE_NAME not in response.cookies
