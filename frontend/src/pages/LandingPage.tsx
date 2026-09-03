import { ArrowRight, CheckCircle2, FileSearch, ShieldCheck } from "lucide-react";
import { Link, Navigate } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { LoadingState } from "../components/States";

export function LandingPage() {
  const { user, loading } = useAuth();
  if (loading) return <LoadingState />;
  if (user) return <Navigate to="/announcements" replace />;
  return (
    <main className="landing">
      <header className="landing-header"><span className="brand-mark">IMT</span><Link className="button button-ghost" to="/login">로그인</Link></header>
      <section className="hero">
        <div className="hero-copy">
          <span className="eyebrow">기업마당 공고 자격 확인</span>
          <h1>지원사업,<br />신청 가능한지부터<br /><em>근거로 확인하세요.</em></h1>
          <p>기업정보를 한 번 입력하면 공고의 필수조건을 원문과 함께 비교해 신청 가능 여부를 빠르게 정리합니다.</p>
          <Link className="button button-primary button-large" to="/login">시작하기 <ArrowRight size={18} /></Link>
        </div>
        <div className="hero-card" aria-label="서비스 주요 기능">
          <div><FileSearch /><span><strong>공고 조건 분석</strong><small>본문과 첨부의 필수조건을 구조화합니다.</small></span></div>
          <div><CheckCircle2 /><span><strong>3단계 판정</strong><small>신청 가능·확인 필요·신청 어려움으로 구분합니다.</small></span></div>
          <div><ShieldCheck /><span><strong>원문 근거 확인</strong><small>모든 조건을 실제 공고 문구와 함께 보여 줍니다.</small></span></div>
        </div>
      </section>
      <p className="landing-privacy">저장된 기업정보 전체와 공고 내용은 자격조건 분석을 위해 Codex로 전송될 수 있습니다.</p>
    </main>
  );
}
