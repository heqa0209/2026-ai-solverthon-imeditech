import { useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";

import { takeReturnTo, useAuth } from "../auth/AuthProvider";
import { api, ApiError } from "../lib/api";

export function LoginPage() {
  const { user, loading, login } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  if (!loading && user) return <Navigate to="/announcements" replace />;

  const submit = async (event: FormEvent) => {
    event.preventDefault(); setError(""); setSubmitting(true);
    try {
      await login(username, password);
      const returnTo = takeReturnTo();
      if (returnTo) navigate(returnTo, { replace: true });
      else {
        const company = await api.company();
        navigate(company.profile?.companyName ? "/announcements" : "/company", { replace: true });
      }
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) setError("아이디 또는 비밀번호를 확인해 주세요.");
      else if (reason instanceof ApiError) setError(reason.body.message);
      else setError("로그인 중 문제가 발생했습니다. 잠시 뒤 다시 시도해 주세요.");
    } finally { setSubmitting(false); }
  };

  return (
    <main className="auth-page">
      <section className="auth-panel">
        <a className="brand-row" href="/"><span className="brand-mark">IMT</span><strong>지원사업 판정</strong></a>
        <div className="auth-heading"><span className="eyebrow">다시 오신 것을 환영합니다</span><h1>로그인</h1><p>관리자가 발급한 계정으로 접속해 주세요.</p></div>
        <form onSubmit={submit} noValidate>
          <label>아이디<input autoFocus autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required /></label>
          <label>비밀번호<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
          {error && <p className="form-alert" role="alert">{error}</p>}
          <button className="button button-primary button-full" disabled={submitting || !username || !password}>{submitting ? "로그인 중…" : "로그인"}</button>
        </form>
        <p className="auth-help">계정이 없거나 비밀번호를 잊으셨다면 운영 관리자에게 문의해 주세요.</p>
      </section>
      <aside className="auth-aside"><blockquote>“지원 조건을 일일이 찾는 대신,<br />근거와 함께 한눈에 확인합니다.”</blockquote></aside>
    </main>
  );
}
