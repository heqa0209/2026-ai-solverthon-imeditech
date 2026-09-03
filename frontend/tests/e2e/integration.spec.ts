import { expect, test } from "@playwright/test";

test.skip(process.env.INTEGRATION_E2E !== "1", "real service integration is opt-in");

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required for integration E2E`);
  return value;
}

test("real FastAPI, CSRF, ETag, fixture evidence and download journey", async ({ page }) => {
  const username = required("E2E_USERNAME");
  const password = required("E2E_PASSWORD");
  let companyRequestHeaders: Record<string, string> | undefined;
  let interestRequestHeaders: Record<string, string> | undefined;

  page.on("request", (request) => {
    if (request.method() === "PUT" && request.url().endsWith("/api/v1/company")) companyRequestHeaders = request.headers();
    if (request.method() === "PUT" && request.url().endsWith("/interest")) interestRequestHeaders = request.headers();
  });

  await page.goto("/login");
  await page.getByLabel("아이디").fill(username);
  await page.getByLabel("비밀번호").fill(password);
  await page.getByRole("button", { name: "로그인" }).click();
  await expect(page).toHaveURL(/\/(announcements|company)$/);

  await page.goto("/company");
  await expect(page.getByRole("heading", { name: "기업정보" })).toBeVisible();
  await page.getByRole("button", { name: "기업정보 저장" }).click();
  await expect(page.getByText(/기업정보를 저장했습니다/)).toBeVisible();
  expect(companyRequestHeaders?.["x-csrf-token"]).toBeTruthy();
  expect(companyRequestHeaders?.["if-match"]).toMatch(/^"\d+"$/);

  await page.goto("/announcements");
  const title = process.env.E2E_EXPECTED_ANNOUNCEMENT_TITLE || "2026 소기업 디지털 전환 지원사업";
  await page.getByRole("button", { name: `${title} 상세 보기` }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.locator(".evidence-list summary").first().click();
  await expect(dialog.locator("blockquote").first()).toBeVisible();

  const fixtureFile = dialog.locator(".file-list a").filter({ hasText: "body.txt" });
  await expect(fixtureFile).toBeVisible();
  const fileHref = await fixtureFile.getAttribute("href");
  expect(fileHref).toBeTruthy();
  const download = await page.request.get(fileHref!);
  expect(download.status()).toBe(200);
  expect(await download.body()).not.toHaveLength(0);

  const interestResponse = page.waitForResponse((response) => response.url().endsWith("/interest") && response.request().method() === "PUT");
  await dialog.getByRole("button", { name: "관심 있음" }).click();
  expect((await interestResponse).status()).toBe(200);
  expect(interestRequestHeaders?.["x-csrf-token"]).toBeTruthy();
});
