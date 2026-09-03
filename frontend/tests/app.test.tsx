import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../src/App";
import { clearCsrfToken } from "../src/lib/api";

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
const listItem = {
  id: "notice-1", announcementVersionId: "version-1", companyProfileVersionId: "company-1", title: "2026 의료기기 기술개발 지원사업",
  agencyName: "중소벤처기업부", recruitmentStartsOn: "2026-09-01", recruitmentEndsOn: "2026-09-30", recruitmentStatus: "OPEN",
  eligibility: "ELIGIBLE", reason: "기업규모와 지역 조건을 충족합니다.", interestStatus: null, decisionFreshness: "CURRENT",
};

afterEach(() => { clearCsrfToken(); vi.restoreAllMocks(); sessionStorage.clear(); });

describe("application routes", () => {
  it("returns an unauthenticated protected route to login", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json({ code: "UNAUTHORIZED", message: "login" }, 401));
    render(<MemoryRouter initialEntries={["/company"]}><App /></MemoryRouter>);
    expect(await screen.findByRole("heading", { name: "로그인" })).toBeInTheDocument();
  });

  it("renders fixed server pagination and opens the shared detail dialog", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) return json({ user: { id: "u1", username: "tester" } });
      if (url.includes("/announcements/notice-1")) return json({
        ...listItem, publishedOn: "2026-09-01", summary: "의료기기 기업의 기술개발을 지원합니다.", explanation: "모든 필수조건을 충족했습니다.", passedTrackKey: "기술개발",
        selectedRoleKey: null, rolePredictions: [], conditions: [{ id: "c1", conditionKey: "c1", groupKey: "g1", trackKey: null, roleKey: null, kind: "MANDATORY", subject: "COMPANY_SCALE", operator: "IN", expectedValue: null, unit: null, referenceDate: null, status: "PASS", usedValue: null, explanation: "중소기업", assumptionCode: null, evidence: [{ sourceName: "공고문.pdf", page: 2, verbatimText: "중소기업을 지원 대상으로 한다." }] }],
        questions: [], files: [], sourceUrl: "https://www.bizinfo.go.kr/example",
      });
      if (url.includes("/announcements?")) return json({ items: [listItem], page: 1, pageSize: 10, total: 11 });
      throw new Error(`Unhandled fetch ${url}`);
    });
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/announcements"]}><App /></MemoryRouter>);
    expect(await screen.findByText("2026 의료기기 기술개발 지원사업")).toBeInTheDocument();
    expect(screen.getByText("1 / 2 페이지")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /상세 보기/ }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("중소기업", { selector: "h4" })).toBeInTheDocument();
    expect(screen.getByText("중소기업을 지원 대상으로 한다.")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /관심 있음|보류|관심 없음/ })).toHaveLength(3);
  });

  it("distinguishes a filtered empty result", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => String(input).endsWith("/auth/me") ? json({ user: { id: "u1", username: "tester" } }) : json({ items: [], page: 1, pageSize: 10, total: 0 }));
    render(<MemoryRouter initialEntries={["/announcements?keyword=없는공고"]}><App /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("조건에 맞는 공고가 없습니다")).toBeInTheDocument());
  });
});
