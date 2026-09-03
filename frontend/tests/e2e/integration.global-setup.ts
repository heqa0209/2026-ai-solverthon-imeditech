import { request, type FullConfig } from "@playwright/test";
import { spawnSync } from "node:child_process";
import { resolve } from "node:path";

const inputFields = [
  "companyName", "businessEntityType", "organizationType", "companyScale", "foundedOn",
  "eligibleRegions", "primaryIndustry", "secondaryIndustries", "annualRevenue", "employeeCount",
  "delinquencyStatus", "certifications", "supportHistory", "capabilityTags", "interestKeywords", "excludedKeywords",
] as const;

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required for integration E2E`);
  return value;
}

function backendCli(args: string[]) {
  const backendDirectory = resolve(process.cwd(), "../backend");
  return spawnSync("uv", ["run", "python", "-m", "app.cli", ...args], {
    cwd: backendDirectory,
    env: process.env,
    encoding: "utf8",
  });
}

export default async function globalSetup(config: FullConfig) {
  if (process.env.INTEGRATION_E2E !== "1") throw new Error("Set INTEGRATION_E2E=1 to authorize the real local integration test setup");
  const username = required("E2E_USERNAME");
  const password = required("E2E_PASSWORD");
  const project = config.projects[0];
  const baseURL = String(project.use.baseURL);

  const create = backendCli(["user", "create", "--username", username, "--password", password]);
  if (create.status !== 0) {
    const reset = backendCli(["user", "reset-password", "--username", username, "--password", password]);
    if (reset.status !== 0) throw new Error("Could not create or reset the isolated E2E user");
  }

  const api = await request.newContext({ baseURL, extraHTTPHeaders: { Origin: new URL(baseURL).origin } });
  const login = await api.post("/api/v1/auth/login", { data: { username, password } });
  if (!login.ok()) throw new Error(`Integration login failed with HTTP ${login.status()}`);
  const company = await api.get("/api/v1/company");
  const csrf = await api.get("/api/v1/auth/csrf");
  if (!company.ok() || !csrf.ok()) throw new Error("Integration company/CSRF setup request failed");
  const companyBody = await company.json();
  const csrfBody = await csrf.json();
  const current = companyBody.profile || {};
  const payload = Object.fromEntries(inputFields.map((field) => [field, current[field] ?? ({
    companyName: "Playwright 통합 테스트 기업",
    eligibleRegions: [], secondaryIndustries: [], certifications: [], supportHistory: [], capabilityTags: [], interestKeywords: [], excludedKeywords: [],
  } as Record<string, unknown>)[field] ?? null]));
  const save = await api.put("/api/v1/company", {
    headers: { "X-CSRF-Token": csrfBody.csrfToken, "If-Match": company.headers()["etag"] || '"0"' },
    data: payload,
  });
  if (!save.ok()) throw new Error(`Integration company setup failed with HTTP ${save.status()}`);
  await api.dispose();

  const fixture = backendCli(["fixture", "load", "--manifest", "../fixtures/demo/manifest.json"]);
  if (fixture.status !== 0 && fixture.status !== 2) throw new Error("Could not load the bounded demo fixture for integration E2E");
}
