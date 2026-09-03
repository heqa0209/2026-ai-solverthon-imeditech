import { Link } from "react-router-dom";
import { useAuth } from "../auth/AuthProvider";

export function NotFoundPage() {
  const { user } = useAuth();
  return <main className="not-found"><span>404</span><h1>페이지를 찾을 수 없습니다</h1><p>입력한 주소가 올바른지 확인해 주세요.</p><Link className="button button-primary" to={user ? "/announcements" : "/"}>{user ? "전체 공고로 돌아가기" : "처음으로 돌아가기"}</Link></main>;
}
