import { describe, expect, it } from "vitest";

import { buildCompanyInput, validateCompany } from "../src/pages/CompanyPage";

const draft = {
  companyName: "  아이엠티  ", businessEntityType: null, organizationType: null, companyScale: "UNKNOWN" as const,
  foundedOn: null, eligibleRegions: [{ code: "11", name: "서울특별시" }], primaryIndustry: " 의료기기 제조업 ",
  secondaryIndustries: [], annualRevenue: "1,234,000", employeeCount: "12", delinquencyStatus: null,
  certifications: [], supportHistory: [{ programName: " 사업 A ", year: 2025 }], capabilityTags: [], interestKeywords: [], excludedKeywords: [],
};

describe("company form contract", () => {
  it("builds an allowlisted, normalized PUT payload", () => {
    expect(buildCompanyInput(draft)).toEqual({
      companyName: "아이엠티", businessEntityType: null, organizationType: null, companyScale: "UNKNOWN", foundedOn: null,
      eligibleRegions: [{ code: "11", name: "서울특별시" }], primaryIndustry: "의료기기 제조업", secondaryIndustries: [],
      annualRevenue: 1234000, employeeCount: 12, delinquencyStatus: null, certifications: [],
      supportHistory: [{ programName: "사업 A", year: 2025 }], capabilityTags: [], interestKeywords: [], excludedKeywords: [],
    });
  });

  it("blocks missing company name and future founded date", () => {
    const errors = validateCompany({ ...draft, companyName: " ", foundedOn: "2999-01-01" });
    expect(errors.companyName).toContain("기업명");
    expect(errors.foundedOn).toContain("오늘");
  });

  it("blocks revenue integers that JSON cannot represent exactly", () => {
    const unsafeDraft = { ...draft, annualRevenue: "9,007,199,254,740,992" };
    const errors = validateCompany(unsafeDraft);
    expect(errors.annualRevenue).toContain("정확하게 처리할 수 있는 범위");
    expect(() => buildCompanyInput(unsafeDraft)).toThrow(RangeError);
  });
});
