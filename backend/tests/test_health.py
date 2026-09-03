from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import health


def test_liveness_is_public_and_minimal(client: TestClient) -> None:
    response = client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_checks_without_secrets_or_paths(client: TestClient) -> None:
    response = client.get("/api/v1/ops/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert set(body["checks"]) == {
        "database",
        "sourceStorage",
        "bizinfoCredential",
        "codexCli",
        "worker",
    }
    serialized = response.text
    assert "test-key" not in serialized
    assert "/Users/" not in serialized


class FakeProcess:
    def __init__(self, *, returncode: int = 0, blocked: bool = False) -> None:
        self.returncode = returncode
        self.blocked = blocked
        self.killed = False

    async def wait(self) -> int:
        if self.blocked and not self.killed:
            await asyncio.Event().wait()
        return self.returncode

    def kill(self) -> None:
        self.killed = True


@pytest.mark.asyncio
async def test_codex_readiness_requires_successful_login_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    success = FakeProcess(returncode=0)
    monkeypatch.setattr(health.shutil, "which", lambda _: "/test/codex")

    async def create_success(*args, **kwargs):
        assert args == ("/test/codex", "login", "status")
        assert kwargs["stdout"] == kwargs["stderr"]
        return success

    monkeypatch.setattr(health.asyncio, "create_subprocess_exec", create_success)
    assert await health._codex_login_status(timeout_seconds=0.1) is True


@pytest.mark.asyncio
async def test_codex_readiness_times_out_kills_process_and_hides_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = FakeProcess(blocked=True)
    monkeypatch.setattr(health.shutil, "which", lambda _: "/secret/path/codex")

    async def create_blocked(*_args, **_kwargs):
        return blocked

    monkeypatch.setattr(health.asyncio, "create_subprocess_exec", create_blocked)
    assert await health._codex_login_status(timeout_seconds=0.001) is False
    assert blocked.killed is True
