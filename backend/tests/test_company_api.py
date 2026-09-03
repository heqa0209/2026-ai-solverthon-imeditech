from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import CompanyProfileVersion


def mutation_headers(csrf: str) -> dict[str, str]:
    return {"Origin": "http://testserver", "X-CSRF-Token": csrf}


def test_company_name_only_creates_immutable_versions_with_etag(
    authenticated_client: tuple[TestClient, str], session_factory
) -> None:
    client, csrf = authenticated_client
    initial = client.get("/api/v1/company")
    assert initial.status_code == 200
    assert initial.json() == {"profile": None, "version": 0}
    assert initial.headers["ETag"] == '"0"'

    created = client.put(
        "/api/v1/company",
        json={"companyName": " 아이메디텍 "},
        headers={**mutation_headers(csrf), "If-Match": '"0"'},
    )
    assert created.status_code == 200
    assert created.json()["profile"]["companyName"] == "아이메디텍"
    assert created.json()["version"] == 1
    assert created.headers["ETag"] == '"1"'

    async def raw_input() -> dict:
        async with session_factory() as db:
            version = await db.scalar(
                select(CompanyProfileVersion).where(CompanyProfileVersion.version == 1)
            )
            return version.raw_input

    assert asyncio.run(raw_input())["companyName"] == " 아이메디텍 "

    csrf = client.get("/api/v1/auth/csrf").json()["csrfToken"]
    updated = client.put(
        "/api/v1/company",
        json={"companyName": "아이메디텍 2"},
        headers={**mutation_headers(csrf), "If-Match": '"1"'},
    )
    assert updated.status_code == 200
    versions = client.get("/api/v1/company/versions").json()["items"]
    assert [item["version"] for item in versions] == [2, 1]
    assert versions[1]["profile"]["companyName"] == "아이메디텍"


def test_company_mutation_requires_csrf_origin_and_current_etag(
    authenticated_client: tuple[TestClient, str],
) -> None:
    client, csrf = authenticated_client
    no_origin = client.put(
        "/api/v1/company",
        json={"companyName": "아이메디텍"},
        headers={"If-Match": '"0"', "X-CSRF-Token": csrf},
    )
    assert no_origin.status_code == 403

    created = client.put(
        "/api/v1/company",
        json={"companyName": "아이메디텍"},
        headers={**mutation_headers(csrf), "If-Match": '"0"'},
    )
    assert created.status_code == 200
    csrf = client.get("/api/v1/auth/csrf").json()["csrfToken"]
    conflict = client.put(
        "/api/v1/company",
        json={"companyName": "충돌"},
        headers={**mutation_headers(csrf), "If-Match": '"0"'},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "COMPANY_VERSION_CONFLICT"


def test_company_rejects_get_fields_and_deprecated_fields(
    authenticated_client: tuple[TestClient, str],
) -> None:
    client, csrf = authenticated_client
    for payload in (
        {"companyName": "아이메디텍", "version": 4},
        {"companyName": "아이메디텍", "companyClassification": "SME"},
    ):
        response = client.put(
            "/api/v1/company",
            json=payload,
            headers={**mutation_headers(csrf), "If-Match": '"0"'},
        )
        assert response.status_code == 422


def test_company_list_normalization_and_bounds() -> None:
    from app.schemas import CompanyProfileInput

    profile = CompanyProfileInput(
        companyName="아이메디텍",
        certifications=[" ISO 9001 ", "iso   9001", "벤처기업"],
        supportHistory=[
            {"programName": " 지원 사업 ", "year": 2025},
            {"programName": "지원  사업", "year": 2025},
        ],
    )
    assert profile.certifications == ["ISO 9001", "벤처기업"]
    assert len(profile.supportHistory) == 1
