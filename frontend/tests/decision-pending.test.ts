import { describe, expect, it } from "vitest";

import { decisionFingerprint, pendingDecisionState } from "../src/lib/decisionPending";
import type { AnnouncementDetail } from "../src/types";

const detail = {
  decisionId: "decision-old",
  decisionPublishedAt: "2026-09-03T09:00:00+09:00",
  eligibility: "ELIGIBLE",
  reason: "기존 판정",
  explanation: "기존 설명",
  passedTrackKey: "TRACK_A",
  conditions: [{ id: "condition-1", status: "PASS", explanation: "충족" }],
} as AnnouncementDetail;

describe("queued decision publication tracking", () => {
  it("does not treat unrelated UI state or CURRENT freshness as a new decision", () => {
    const baseline = decisionFingerprint(detail);
    const roleOnlyChanged = { ...detail, selectedRoleKey: "LEAD", decisionFreshness: "CURRENT" } as AnnouncementDetail;
    expect(decisionFingerprint(roleOnlyChanged)).toBe(baseline);
    expect(pendingDecisionState({ baseline, detail: roleOnlyChanged, deadline: 40_000, now: 20_000 })).toBe("PENDING");
  });

  it("completes only when decision output changes and otherwise times out explicitly", () => {
    const baseline = decisionFingerprint(detail);
    expect(pendingDecisionState({ baseline, detail: { ...detail, reason: "새 판정" }, deadline: 40_000, now: 20_000 })).toBe("COMPLETED");
    expect(pendingDecisionState({ baseline, detail: { ...detail, decisionId: "decision-new" }, deadline: 40_000, now: 20_000 })).toBe("COMPLETED");
    expect(pendingDecisionState({ baseline, detail, deadline: 40_000, now: 40_001 })).toBe("TIMED_OUT");
  });
});
