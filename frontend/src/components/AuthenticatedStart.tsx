import { useQuery } from "@tanstack/react-query";
import { Navigate } from "react-router-dom";

import { api } from "../lib/api";
import { ErrorState, LoadingState } from "./States";

export function AuthenticatedStart() {
  const query = useQuery({ queryKey: ["company", "start"], queryFn: api.company, staleTime: 0 });
  if (query.isLoading) return <LoadingState label="첫 화면을 준비하는 중입니다" />;
  if (query.isError) return <ErrorState message="기업정보 상태를 확인하지 못했습니다." action={<button className="button button-secondary" onClick={() => void query.refetch()}>다시 시도</button>} />;
  return <Navigate to={query.data?.profile?.companyName ? "/announcements" : "/company"} replace />;
}
