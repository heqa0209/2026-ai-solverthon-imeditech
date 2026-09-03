import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";

import { AuthProvider, rememberReturnTo, useAuth } from "./auth/AuthProvider";
import { LoadingState } from "./components/States";
import { AnnouncementsPage } from "./pages/AnnouncementsPage";
import { CompanyPage } from "./pages/CompanyPage";
import { LandingPage } from "./pages/LandingPage";
import { LoginPage } from "./pages/LoginPage";
import { NotFoundPage } from "./pages/NotFoundPage";

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1, staleTime: 15_000, refetchOnWindowFocus: false } } });

function ProtectedRoute() {
  const { user, loading } = useAuth();
  const location = useLocation();
  if (loading) return <LoadingState label="로그인 상태를 확인하는 중입니다" />;
  if (!user) { rememberReturnTo(`${location.pathname}${location.search}`); return <Navigate to="/login" replace />; }
  return <Outlet />;
}

function AppRoutes() {
  return <Routes>
    <Route path="/" element={<LandingPage />} />
    <Route path="/login" element={<LoginPage />} />
    <Route element={<ProtectedRoute />}>
      <Route path="/announcements" element={<AnnouncementsPage />} />
      <Route path="/announcements/:id" element={<AnnouncementsPage />} />
      <Route path="/interests" element={<AnnouncementsPage interests />} />
      <Route path="/company" element={<CompanyPage />} />
    </Route>
    <Route path="*" element={<NotFoundPage />} />
  </Routes>;
}

export function App() {
  return <QueryClientProvider client={queryClient}><AuthProvider><AppRoutes /></AuthProvider></QueryClientProvider>;
}
