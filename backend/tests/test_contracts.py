from datetime import date

import pytest
from pydantic import ValidationError

from app.schemas import CompanyProfileInput


def test_company_name_only_is_valid() -> None:
    value = CompanyProfileInput(companyName="아이메디텍")
    assert value.companyName == "아이메디텍"
    assert value.eligibleRegions == []


def test_deprecated_company_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        CompanyProfileInput(companyName="아이메디텍", companyClassification="SME")


def test_future_founded_date_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CompanyProfileInput(companyName="아이메디텍", foundedOn=date(2999, 1, 1))
