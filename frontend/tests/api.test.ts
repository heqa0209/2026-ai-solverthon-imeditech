import { afterEach, describe, expect, it, vi } from "vitest";

import { api, clearCsrfToken } from "../src/lib/api";
import type { CompanyProfileInput } from "../src/types";

const profile: CompanyProfileInput = {
  companyName: "아이엠티", businessEntityType: null, organizationType: null, companyScale: null, foundedOn: null,
  eligibleRegions: [], primaryIndustry: null, secondaryIndustries: [], annualRevenue: null, employeeCount: null,
  delinquencyStatus: null, certifications: [], supportHistory: [], capabilityTags: [], interestKeywords: [], excludedKeywords: [],
};

afterEach(() => { clearCsrfToken(); vi.restoreAllMocks(); });

describe("API client", () => {
  it("sends credentials, CSRF, If-Match and only the explicit company input", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrfToken: "csrf-123" }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ profile, version: 1 }), { status: 200, headers: { "Content-Type": "application/json", ETag: '"1"' } }));

    const result = await api.updateCompany(profile, '"0"');

    expect(result.etag).toBe('"1"');
    const [, options] = fetchMock.mock.calls[1];
    const headers = new Headers(options?.headers);
    expect(options?.credentials).toBe("include");
    expect(headers.get("X-CSRF-Token")).toBe("csrf-123");
    expect(headers.get("If-Match")).toBe('"0"');
    expect(JSON.parse(String(options?.body))).toEqual(profile);
    expect(JSON.parse(String(options?.body))).not.toHaveProperty("version");
  });

  it("does not redirect a failed login as a session timeout", async () => {
    const unauthorized = vi.fn();
    window.addEventListener("app:unauthorized", unauthorized);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ code: "INVALID_LOGIN", message: "invalid" }), { status: 401, headers: { "Content-Type": "application/json" } }));
    await expect(api.login("tester", "wrong")).rejects.toMatchObject({ status: 401 });
    expect(unauthorized).not.toHaveBeenCalled();
    window.removeEventListener("app:unauthorized", unauthorized);
  });
});
