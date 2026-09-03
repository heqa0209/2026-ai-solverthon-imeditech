import { Search, SlidersHorizontal, X } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { AnnouncementCard } from "../components/AnnouncementCard";
import { AnnouncementDetailDialog } from "../components/AnnouncementDetailDialog";
import { AppShell } from "../components/AppShell";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { api, ApiError } from "../lib/api";

function pages(current: number, total: number) {
  const start = Math.max(1, Math.min(current - 2, total - 4));
  return Array.from({ length: Math.min(5, total) }, (_, index) => start + index);
}

export function AnnouncementsPage({ interests = false }: { interests?: boolean }) {
  const params = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const backgroundPath = (location.state as { backgroundPath?: string; scrollY?: number } | null)?.backgroundPath;
  const effectiveInterests = interests || backgroundPath?.startsWith("/interests") || false;
  const [keywordDraft, setKeywordDraft] = useState(searchParams.get("keyword") || "");
  const queryString = searchParams.toString();

  const apiParams = useMemo(() => {
    const next = new URLSearchParams(queryString);
    next.set("page", next.get("page") || "1"); next.set("pageSize", "10");
    if (effectiveInterests && !next.has("interestStatus")) next.set("interestStatus", "ANY_SET");
    return next;
  }, [queryString, effectiveInterests]);

  const query = useQuery({
    queryKey: ["announcements", apiParams.toString()], queryFn: () => api.announcements(apiParams),
    refetchInterval: (result) => result.state.data?.items.some((item) => item.decisionFreshness !== "CURRENT") ? 5000 : false,
  });
  const page = Number(searchParams.get("page") || 1);
  const totalPages = Math.max(1, Math.ceil((query.data?.total || 0) / 10));
  const hasFilters = Boolean(searchParams.get("keyword") || searchParams.get("eligibility") || searchParams.get("recruitmentStatus") || (!effectiveInterests && searchParams.get("interestStatus")));

  useEffect(() => {
    if (!params.id && typeof (location.state as { scrollY?: number } | null)?.scrollY === "number") window.scrollTo({ top: (location.state as { scrollY: number }).scrollY });
  }, [location.state, params.id]);

  const update = (key: string, value: string) => {
    const next = new URLSearchParams(searchParams); if (value) next.set(key, value); else next.delete(key); next.set("page", "1"); setSearchParams(next);
  };
  const submitSearch = (event: FormEvent) => { event.preventDefault(); update("keyword", keywordDraft.trim()); };
  const open = (id: string) => navigate(`/announcements/${id}`, { state: { backgroundPath: `${effectiveInterests ? "/interests" : "/announcements"}${searchParams.size ? `?${searchParams}` : ""}`, scrollY: window.scrollY } });
  const close = () => {
    if (backgroundPath) navigate(backgroundPath, { replace: true, state: { scrollY: (location.state as { scrollY?: number })?.scrollY || 0 } });
    else navigate("/announcements", { replace: true });
  };

  return <AppShell title={effectiveInterests ? "관심 공고" : "전체 공고"} description={effectiveInterests ? "관심 상태를 정한 공고를 모아 봅니다. 자격 판정은 관심 상태와 무관합니다." : "기업정보와 공고의 필수조건을 비교한 결과입니다."}>
    <section className="filters" aria-label="공고 필터">
      <form className="keyword-search" onSubmit={submitSearch}><Search size={18} /><input aria-label="공고 키워드" value={keywordDraft} onChange={(e) => setKeywordDraft(e.target.value)} placeholder="제목, 기관, 요약, 조건 검색" /><button className="button button-primary">검색</button></form>
      <div className="filter-row"><SlidersHorizontal size={17} aria-hidden="true" /><label>신청 판정<select value={searchParams.get("eligibility") || ""} onChange={(e) => update("eligibility", e.target.value)}><option value="">전체</option><option value="ELIGIBLE">신청 가능</option><option value="NEEDS_CONFIRMATION">확인 필요</option><option value="INELIGIBLE">신청 어려움</option></select></label><label>모집 상태<select value={searchParams.get("recruitmentStatus") || ""} onChange={(e) => update("recruitmentStatus", e.target.value)}><option value="">전체</option><option value="OPEN">모집 중</option><option value="CLOSED">마감</option><option value="UNKNOWN">기간 확인 필요</option></select></label>{!effectiveInterests && <label>관심 상태<select value={searchParams.get("interestStatus") || ""} onChange={(e) => update("interestStatus", e.target.value)}><option value="">전체</option><option value="INTERESTED">관심 있음</option><option value="ON_HOLD">보류</option><option value="NOT_INTERESTED">관심 없음</option></select></label>}{hasFilters && <button type="button" className="clear-filters" onClick={() => { setKeywordDraft(""); setSearchParams({ page: "1" }); }}><X size={15} />필터 초기화</button>}</div>
    </section>
    {query.isLoading && <LoadingState label="공고를 불러오는 중입니다" />}
    {query.isError && <ErrorState message={query.error instanceof ApiError ? query.error.body.message : "공고 목록을 불러오지 못했습니다."} action={<button className="button button-secondary" onClick={() => void query.refetch()}>다시 시도</button>} />}
    {query.data && <>{query.data.items.length === 0 ? <EmptyState filtered={hasFilters || effectiveInterests} action={hasFilters ? <button className="button button-secondary" onClick={() => { setKeywordDraft(""); setSearchParams({ page: "1" }); }}>필터 초기화</button> : undefined} /> : <><div className="list-meta"><strong>전체 {query.data.total.toLocaleString("ko-KR")}건</strong><span>{page} / {totalPages} 페이지</span></div><div className="announcement-grid">{query.data.items.map((item) => <AnnouncementCard key={item.id} item={item} onOpen={() => open(item.id)} />)}</div><nav className="pagination" aria-label="페이지 이동"><button disabled={page <= 1} onClick={() => update("page", String(page - 1))}>이전</button>{pages(page, totalPages).map((number) => <button key={number} className={page === number ? "active" : ""} aria-current={page === number ? "page" : undefined} onClick={() => update("page", String(number))}>{number}</button>)}<button disabled={page >= totalPages} onClick={() => update("page", String(page + 1))}>다음</button></nav></>}</>}
    {params.id && <AnnouncementDetailDialog id={params.id} onClose={close} />}
  </AppShell>;
}
