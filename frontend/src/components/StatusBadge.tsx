import type { ConditionStatus, DecisionFreshness, InterestStatus, Verdict } from "../types";

const verdictLabels: Record<Verdict, string> = { ELIGIBLE: "신청 가능", NEEDS_CONFIRMATION: "확인 필요", INELIGIBLE: "신청 어려움" };
const conditionLabels: Record<ConditionStatus, string> = { PASS: "충족", UNKNOWN: "확인 필요", FAIL: "불충족", NOT_APPLICABLE: "해당 없음" };
export const interestLabels: Record<InterestStatus, string> = { INTERESTED: "관심 있음", ON_HOLD: "보류", NOT_INTERESTED: "관심 없음" };

export function VerdictBadge({ verdict }: { verdict: Verdict }) { return <span className={`badge verdict-${verdict.toLowerCase()}`}>{verdictLabels[verdict]}</span>; }
export function ConditionBadge({ status }: { status: ConditionStatus }) { return <span className={`condition-badge condition-${status.toLowerCase()}`}>{conditionLabels[status]}</span>; }
export function InterestBadge({ status }: { status: InterestStatus }) { return <span className={`interest-label interest-${status.toLowerCase()}`}>{interestLabels[status]}</span>; }
export function FreshnessNotice({ freshness }: { freshness: DecisionFreshness }) {
  if (freshness === "CURRENT") return null;
  const text = freshness === "COMPANY_CHANGED" ? "기업정보 변경 전 결과" : freshness === "ANNOUNCEMENT_CHANGED" ? "공고 변경 전 결과" : "변경 전 결과 · 새 판정 준비 중";
  return <span className="freshness-notice">{text}</span>;
}
