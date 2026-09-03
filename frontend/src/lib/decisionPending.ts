import type { AnnouncementDetail } from "../types";

export type PendingDecisionPhase = "PENDING" | "COMPLETED" | "TIMED_OUT";

export function decisionFingerprint(detail: AnnouncementDetail): string {
  return JSON.stringify({
    decisionId: detail.decisionId,
    decisionPublishedAt: detail.decisionPublishedAt,
    eligibility: detail.eligibility,
    reason: detail.reason,
    explanation: detail.explanation,
    passedTrackKey: detail.passedTrackKey,
    conditions: detail.conditions.map((condition) => ({
      id: condition.id,
      status: condition.status,
      explanation: condition.explanation,
      usedValue: condition.usedValue,
      assumptionCode: condition.assumptionCode,
    })),
  });
}

export function pendingDecisionState({ baseline, detail, deadline, now }: {
  baseline: string;
  detail: AnnouncementDetail | undefined;
  deadline: number;
  now: number;
}): PendingDecisionPhase {
  if (detail && decisionFingerprint(detail) !== baseline) return "COMPLETED";
  return now >= deadline ? "TIMED_OUT" : "PENDING";
}
