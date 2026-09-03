import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { api, ApiError, clearCsrfToken } from "../lib/api";
import type { User } from "../types";

type AuthContextValue = {
  user: User | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<User | null>;
};

const AuthContext = createContext<AuthContextValue | null>(null);
const RETURN_KEY = "solverthon:return-to";

export function rememberReturnTo(path: string) {
  if (path.startsWith("/")) sessionStorage.setItem(RETURN_KEY, path);
}

export function takeReturnTo() {
  const path = sessionStorage.getItem(RETURN_KEY);
  sessionStorage.removeItem(RETURN_KEY);
  return path && path.startsWith("/") ? path : null;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();
  const location = useLocation();

  const refresh = async () => {
    try {
      const result = await api.me();
      setUser(result.user);
      return result.user;
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setUser(null);
        clearCsrfToken();
        return null;
      }
      throw error;
    }
  };

  useEffect(() => {
    refresh().finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    const onUnauthorized = () => {
      if (location.pathname !== "/login") rememberReturnTo(`${location.pathname}${location.search}`);
      setUser(null);
      clearCsrfToken();
      navigate("/login", { replace: true });
    };
    window.addEventListener("app:unauthorized", onUnauthorized);
    return () => window.removeEventListener("app:unauthorized", onUnauthorized);
  }, [location.pathname, location.search, navigate]);

  return (
    <AuthContext.Provider value={{
      user, loading, refresh,
      login: async (username, password) => { const result = await api.login(username, password); setUser(result.user); },
      logout: async () => { await api.logout(); setUser(null); navigate("/", { replace: true }); },
    }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}
