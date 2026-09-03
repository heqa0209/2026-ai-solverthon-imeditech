import { ArrowUpRight, CalendarDays } from "lucide-react";

import { formatDate } from "../lib/format";
import type { AnnouncementListItem } from "../types";
import { FreshnessNotice, InterestBadge, VerdictBadge } from "./StatusBadge";

export function AnnouncementCard({ item, onOpen }: { item: AnnouncementListItem; onOpen: () => void }) {
  return (
    <article className="announcement-card">
      <button type="button" className="card-hit" onClick={onOpen} aria-label={`${item.title} 상세 보기`} />
      <div className="card-top"><VerdictBadge verdict={item.eligibility} />{item.interestStatus && <InterestBadge status={item.interestStatus} />}</div>
      <h2>{item.title}</h2>
      <p className="organization">{item.agencyName || "기관 확인 필요"}</p>
      <div className="period"><CalendarDays size={16} aria-hidden="true" /><span>{formatDate(item.recruitmentStartsOn)} ~ {formatDate(item.recruitmentEndsOn)}</span>{item.recruitmentStatus === "UNKNOWN" && <b>기간 확인 필요</b>}</div>
      <p className="verdict-reason">{item.reason || "판정 이유를 확인해 주세요."}</p>
      <div className="card-bottom"><FreshnessNotice freshness={item.decisionFreshness} /><span className="detail-link">상세 보기 <ArrowUpRight size={15} /></span></div>
    </article>
  );
}
