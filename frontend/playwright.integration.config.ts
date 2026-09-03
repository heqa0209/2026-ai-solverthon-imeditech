import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.E2E_BASE_URL || "http://localhost:4173";
const startFrontend = process.env.E2E_SKIP_FRONTEND_SERVER !== "1";
const startBackend = process.env.E2E_START_BACKEND === "1";
const webServers = [
  ...(startBackend ? [{
    command: "uv run uvicorn app.main:app --host 127.0.0.1 --port 8000",
    cwd: "../backend",
    url: "http://127.0.0.1:8000/api/v1/health/live",
    reuseExistingServer: !process.env.CI,
  }] : []),
  ...(startFrontend ? [{
    command: "npm run build && npm exec vite preview -- --host 127.0.0.1 --port 4173",
    url: baseURL,
    reuseExistingServer: !process.env.CI,
  }] : []),
];

export default defineConfig({
  testDir: "./tests/e2e",
  testMatch: "integration.spec.ts",
  globalSetup: "./tests/e2e/integration.global-setup.ts",
  timeout: 30_000,
  use: { baseURL, trace: "on-first-retry" },
  webServer: webServers.length ? webServers : undefined,
  projects: [{ name: "integration-chrome", use: { ...devices["Desktop Chrome"], channel: "chrome" } }],
});
