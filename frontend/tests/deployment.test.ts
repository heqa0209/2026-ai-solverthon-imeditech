import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

describe("Vercel routing", () => {
  it("routes relative API calls before the SPA fallback", () => {
    const config = JSON.parse(readFileSync(join(process.cwd(), "vercel.json"), "utf8"));
    expect(config.rewrites[0]).toEqual({
      source: "/api/v1/:path*",
      destination: "https://api.ai-solverthon-2026-imt.party/api/v1/:path*",
    });
    expect(config.rewrites[1]).toEqual({ source: "/(.*)", destination: "/index.html" });
  });
});
