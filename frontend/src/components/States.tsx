import { AlertCircle, Inbox, LoaderCircle } from "lucide-react";
import type { ReactNode } from "react";

export function LoadingState({ label = "불러오는 중입니다" }: { label?: string }) {
  return <div className="state-panel" role="status"><LoaderCircle className="spin" /><strong>{label}</strong><p>잠시만 기다려 주세요.</p></div>;
}

export function ErrorState({ message, action }: { message: string; action?: ReactNode }) {
  return <div className="state-panel state-error" role="alert"><AlertCircle /><strong>정보를 불러오지 못했습니다</strong><p>{message}</p>{action}</div>;
}

export function EmptyState({ filtered = false, action }: { filtered?: boolean; action?: ReactNode }) {
  return <div className="state-panel"><Inbox /><strong>{filtered ? "조건에 맞는 공고가 없습니다" : "아직 수집된 공고가 없습니다"}</strong><p>{filtered ? "검색어나 필터를 바꿔 다시 확인해 보세요." : "공고 수집이 끝나면 이곳에 표시됩니다."}</p>{action}</div>;
}
