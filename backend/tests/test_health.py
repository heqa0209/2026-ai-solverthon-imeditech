from fastapi.testclient import TestClient


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
