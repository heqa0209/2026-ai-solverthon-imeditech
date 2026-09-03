import { expect, test } from "@playwright/test";

const item = {
  id: "notice-1", announcementVersionId: "version-1", companyProfileVersionId: "company-1", title: "2026 의료기기 기술개발 지원사업",
  agencyName: "중소벤처기업부", recruitmentStartsOn: "2026-09-01", recruitmentEndsOn: "2026-09-30", recruitmentStatus: "OPEN",
  eligibility: "ELIGIBLE", reason: "기업규모와 지역 조건을 충족합니다.", interestStatus: "INTERESTED", decisionFreshness: "CURRENT",
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/auth/me")) return route.fulfill({ json: { user: { id: "u1", username: "tester" } } });
    if (url.pathname.endsWith("/auth/csrf")) return route.fulfill({ json: { csrfToken: "test-token" } });
    if (url.pathname.endsWith("/announcements/notice-1/interest")) return route.fulfill({ json: { status: "ON_HOLD", updatedAt: "2026-09-03T01:00:00+09:00" } });
    if (url.pathname.endsWith("/announcements/notice-1")) return route.fulfill({ json: { ...item, publishedOn: "2026-09-01", summary: "의료기기 기업의 기술개발을 지원합니다.", explanation: "필수조건을 충족했습니다.", passedTrackKey: "기술개발", selectedRoleKey: null, rolePredictions: [], conditions: [{ id: "c1", conditionKey: "c1", groupKey: "g1", trackKey: null, roleKey: null, kind: "MANDATORY", subject: "COMPANY_SCALE", operator: "IN", expectedValue: null, unit: null, referenceDate: null, status: "PASS", usedValue: null, explanation: "중소기업", assumptionCode: null, evidence: [{ sourceName: "공고문.pdf", page: 2, verbatimText: "중소기업을 지원 대상으로 한다." }] }], questions: [], files: [], sourceUrl: "https://www.bizinfo.go.kr/example" } });
    if (url.pathname.endsWith("/announcements")) return route.fulfill({ json: { items: [item], page: 1, pageSize: 10, total: 1 } });
    return route.fulfill({ status: 404, json: { code: "NOT_FOUND", message: "not found" } });
  });
});

test("announcement evidence and interest flow", async ({ page }) => {
  await page.goto("/announcements");
  await expect(page.getByRole("heading", { name: "전체 공고" })).toBeVisible();
  await page.getByRole("button", { name: /2026 의료기기 기술개발 지원사업 상세 보기/ }).click();
  await expect(page).toHaveURL(/\/announcements\/notice-1/);
  await page.getByText("공고문.pdf · 2쪽").click();
  await expect(page.getByText("중소기업을 지원 대상으로 한다.")).toBeVisible();
  await page.getByRole("button", { name: "보류" }).click();
});

test("mobile navigation exposes the same pages", async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.includes("mobile"), "mobile-only navigation assertion");
  await page.goto("/announcements");
  await page.getByRole("button", { name: "메뉴 열기" }).click();
  await expect(page.getByRole("navigation", { name: "주요 메뉴" })).toBeVisible();
  await expect(page.getByRole("link", { name: "기업정보" })).toBeVisible();
});
