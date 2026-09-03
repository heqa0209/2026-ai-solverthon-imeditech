import * as Dialog from "@radix-ui/react-dialog";
import { ExternalLink, FileText, RefreshCw, X } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { api, ApiError, fileUrl } from "../lib/api";
import { decisionFingerprint, pendingDecisionState } from "../lib/decisionPending";
import { formatDate, formatFileSize } from "../lib/format";
import type { AnnouncementQuestion, InterestStatus } from "../types";
import { ErrorState, LoadingState } from "./States";
import { ConditionBadge, FreshnessNotice, interestLabels, VerdictBadge } from "./StatusBadge";

const kindLabels = { MANDATORY: "필수조건", PREFERENCE: "우대·가점", GUIDANCE: "안내", POST_AWARD: "신청 후 이행사항" } as const;

function EvidenceBlock({ evidence }: { evidence: AnnouncementQuestion["evidence"] }) {
  if (!evidence.length) return <p className="evidence-missing">원문 근거 확인 필요</p>;
  return <div className="evidence-list">{evidence.map((item, index) => <details key={`${item.sourceName}-${item.page}-${index}`}><summary>{item.sourceName || "공고 본문"}{item.page ? ` · ${item.page}쪽` : ""}</summary><blockquote>{item.verbatimText}</blockquote></details>)}</div>;
}

function QuestionForm({ question, announcementId, announcementVersionId, onQueued }: { question: AnnouncementQuestion; announcementId: string; announcementVersionId: string; onQueued: (requestId: string) => void }) {
  const [value, setValue] = useState("");
  const [source, setSource] = useState("USER_VERIFIED");
  const [memo, setMemo] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault(); if (!value) return; setSubmitting(true); setError("");
    try {
      const parsedValue = question.valueType === "NUMBER" || question.valueType === "INTEGER" ? Number(value) : question.valueType === "BOOLEAN" ? value === "true" : value;
      const queued = await api.answer(announcementId, { announcementVersionId, conditionId: question.conditionId, value: parsedValue, source, ...(memo.trim() ? { memo: memo.trim() } : {}) });
      onQueued(queued.requestId);
    } catch (reason) { setError(reason instanceof ApiError ? reason.body.message : "답변을 저장하지 못했습니다."); }
    finally { setSubmitting(false); }
  };
  return <form className="question-form" onSubmit={submit}><label>{question.prompt}{question.options?.length ? <select value={value} onChange={(e) => setValue(e.target.value)}><option value="">선택</option>{question.options.map((item) => <option value={item} key={item}>{item}</option>)}</select> : question.valueType === "BOOLEAN" ? <select value={value} onChange={(e) => setValue(e.target.value)}><option value="">선택</option><option value="true">예</option><option value="false">아니요</option></select> : <input type={question.valueType === "DATE" ? "date" : question.valueType === "NUMBER" || question.valueType === "INTEGER" ? "number" : "text"} value={value} onChange={(e) => setValue(e.target.value)} />}</label><label>확인 출처<select value={source} onChange={(e) => setSource(e.target.value)}><option value="USER_VERIFIED">사용자 직접 확인</option><option value="OFFICIAL_DOCUMENT">공식 서류</option><option value="AGENCY_INQUIRY">기관 문의</option></select></label><label>메모 (선택)<input value={memo} onChange={(e) => setMemo(e.target.value)} /></label><EvidenceBlock evidence={question.evidence} />{error && <p className="field-error">{error}</p>}<button className="button button-secondary" disabled={!value || submitting}>{submitting ? "저장 중…" : "답변 저장"}</button></form>;
}

export function AnnouncementDetailDialog({ id, onClose }: { id: string; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [actionError, setActionError] = useState("");
  const [pending, setPending] = useState<{ requestId: string; baseline: string; deadline: number } | null>(null);
  const [pendingOutcome, setPendingOutcome] = useState<"COMPLETED" | "TIMED_OUT" | null>(null);
  const [mutating, setMutating] = useState(false);
  const query = useQuery({ queryKey: ["announcement", id], queryFn: () => api.announcement(id), refetchInterval: (result) => pending || result.state.data?.decisionFreshness !== "CURRENT" ? 3000 : false });
  const detail = query.data;

  const beginPending = (requestId: string) => {
    if (!detail) return;
    setPending({ requestId, baseline: decisionFingerprint(detail), deadline: Date.now() + 30_000 });
    setPendingOutcome(null);
    void query.refetch();
    void queryClient.invalidateQueries({ queryKey: ["announcements"] });
  };

  useEffect(() => {
    if (!pending) return;
    const phase = pendingDecisionState({ baseline: pending.baseline, detail, deadline: pending.deadline, now: Date.now() });
    if (phase === "COMPLETED") {
      setPending(null); setPendingOutcome("COMPLETED"); void queryClient.invalidateQueries({ queryKey: ["announcements"] }); return;
    }
    if (phase === "TIMED_OUT") { setPending(null); setPendingOutcome("TIMED_OUT"); return; }
    const timeout = window.setTimeout(() => { setPending(null); setPendingOutcome("TIMED_OUT"); }, Math.max(0, pending.deadline - Date.now()));
    return () => window.clearTimeout(timeout);
  }, [detail, pending, queryClient]);
  const setInterest = async (status: InterestStatus) => {
    setMutating(true); setActionError("");
    try { await api.setInterest(id, status); await query.refetch(); await queryClient.invalidateQueries({ queryKey: ["announcements"] }); }
    catch (reason) { setActionError(reason instanceof ApiError ? reason.body.message : "관심 상태를 저장하지 못했습니다."); }
    finally { setMutating(false); }
  };
  const setRole = async (roleKey: string) => {
    if (!detail) return; setMutating(true); setActionError("");
    try { const queued = await api.setRole(id, detail.announcementVersionId, roleKey || null); beginPending(queued.requestId); }
    catch (reason) { setActionError(reason instanceof ApiError ? reason.body.message : "역할을 저장하지 못했습니다."); }
    finally { setMutating(false); }
  };
  const reevaluate = async () => {
    if (!detail) return; setMutating(true); setActionError("");
    try { const queued = await api.reevaluate(id, detail.announcementVersionId); beginPending(queued.requestId); }
    catch (reason) { setActionError(reason instanceof ApiError ? reason.body.message : "재판정을 요청하지 못했습니다."); }
    finally { setMutating(false); }
  };

  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="detail-dialog" aria-describedby={undefined}>
          <Dialog.Title className="sr-only">공고 상세</Dialog.Title>
          <Dialog.Close className="dialog-close" aria-label="상세 닫기"><X /></Dialog.Close>
          {query.isLoading && <LoadingState label="공고 상세를 불러오는 중입니다" />}
          {query.isError && <ErrorState message={query.error instanceof ApiError ? query.error.body.message : "공고 상세를 불러오지 못했습니다."} action={<button className="button button-secondary" onClick={() => void query.refetch()}>다시 시도</button>} />}
          {detail && <>
            <div className="detail-scroll">
              <header className="detail-header"><div className="detail-badges"><VerdictBadge verdict={detail.eligibility} /><FreshnessNotice freshness={detail.decisionFreshness} /></div><h2>{detail.title}</h2><p>{detail.agencyName || "기관 확인 필요"}</p></header>
              {(pending || detail.decisionFreshness !== "CURRENT") && <div className="notice notice-warning" data-request-id={pending?.requestId}><RefreshCw className="spin" size={18} /><span><strong>판정 반영을 확인하는 중입니다.</strong><br />표시 중인 결과는 변경 전 결과입니다.</span></div>}
              {pendingOutcome === "COMPLETED" && <div className="notice notice-success" role="status">새 판정 결과를 반영했습니다.</div>}
              {pendingOutcome === "TIMED_OUT" && <div className="notice notice-error" role="alert"><span><strong>자동 확인 시간이 끝났습니다.</strong><br />새 판정의 공개 여부를 확인할 수 없어 표시 중인 결과를 변경 전 결과로 유지합니다.</span><button type="button" className="button button-secondary" onClick={() => void reevaluate()} disabled={mutating}>다시 판정 요청</button></div>}
              <section className="detail-verdict"><h3>판정 결과</h3><p className="summary">{detail.summary || "공고 요약을 확인해 주세요."}</p><p>{detail.explanation || detail.reason}</p>{detail.passedTrackKey && <p className="passed-tracks"><strong>통과 유형</strong>{detail.passedTrackKey}</p>}</section>
              {detail.rolePredictions.length > 0 && <section className="detail-section"><h3>신청 역할</h3><p>역할에 따라 조건이 달라집니다. 해당하는 역할을 선택해 주세요.</p><div className="role-options">{detail.rolePredictions.map((role) => <label key={role.roleKey}><input type="radio" name="role" checked={detail.selectedRoleKey === role.roleKey} onChange={() => void setRole(role.roleKey)} disabled={mutating} /><span>{role.label}{role.eligibility && <VerdictBadge verdict={role.eligibility} />}</span></label>)}</div>{detail.selectedRoleKey && <button type="button" className="text-button" onClick={() => void setRole("")} disabled={mutating}>역할 선택 해제</button>}</section>}
              <section className="detail-section"><h3>공고 조건 전체</h3>{detail.conditions.length ? <div className="condition-list">{detail.conditions.map((condition) => <article key={condition.id}><div className="condition-heading"><span className="kind-label">{kindLabels[condition.kind]}</span><ConditionBadge status={condition.status} /></div><h4>{condition.explanation || `${condition.subject} · ${condition.operator}`}</h4>{condition.unit && <p>단위: {condition.unit}</p>}{condition.assumptionCode && <p className="assumption">완화 가정 적용 · {condition.assumptionCode}</p>}<EvidenceBlock evidence={condition.evidence} /></article>)}</div> : <p className="muted">추출된 조건이 없어 원문 확인이 필요합니다.</p>}</section>
              {detail.questions.length > 0 && <section className="detail-section"><h3>추가 확인 질문</h3><p>확인 필요 조건에 답하면 이 공고만 다시 판정합니다. 공통 정보는 <Link className="inline-link" to="/company">기업정보</Link>에서 수정할 수 있습니다.</p>{detail.questions.map((question) => <QuestionForm key={question.conditionId} question={question} announcementId={id} announcementVersionId={detail.announcementVersionId} onQueued={beginPending} />)}</section>}
              <section className="detail-section basic-info"><div className="section-title-row"><h3>공고 기본정보</h3><button type="button" className="text-button" onClick={() => void reevaluate()} disabled={mutating}><RefreshCw size={14} />현재 정보로 다시 판정</button></div><dl><div><dt>주관기관</dt><dd>{detail.agencyName || "확인 필요"}</dd></div><div><dt>신청 기간</dt><dd>{formatDate(detail.recruitmentStartsOn)} ~ {formatDate(detail.recruitmentEndsOn)}</dd></div><div><dt>공고 버전</dt><dd>{detail.announcementVersionId}</dd></div></dl></section>
              <section className="detail-section"><h3>첨부와 원문</h3><div className="file-list">{detail.files.map((file) => file.downloadStatus === "SUCCEEDED" ? <a href={fileUrl(id, file.id)} target="_blank" rel="noreferrer" key={file.id}><FileText /><span><strong>{file.name}</strong><small>{formatFileSize(file.sizeBytes)} · 저장됨</small></span><ExternalLink size={16} /></a> : <div className="file-unavailable" key={file.id}><FileText /><span><strong>{file.name}</strong><small>{formatFileSize(file.sizeBytes)} · {file.failureCode || "열 수 없음"}</small></span></div>)}<a href={detail.sourceUrl} target="_blank" rel="noreferrer"><ExternalLink /><span><strong>기업마당 원문 열기</strong><small>새 탭에서 원문을 확인합니다.</small></span><ExternalLink size={16} /></a></div></section>
            </div>
            <div className="detail-footer">
              {actionError && <p role="alert">{actionError}</p>}
              <div className="interest-actions">{(["INTERESTED", "ON_HOLD", "NOT_INTERESTED"] as InterestStatus[]).map((status) => <button key={status} type="button" disabled={mutating} className={detail.interestStatus === status ? "selected" : ""} onClick={() => void setInterest(status)}>{interestLabels[status]}</button>)}</div>
            </div>
          </>}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
