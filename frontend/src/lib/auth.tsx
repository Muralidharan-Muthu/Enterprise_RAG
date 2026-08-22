"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";

// ── Types ───────────────────────────────────────────────────────────────────

export interface User {
  id: string;
  username: string;
  email: string;
  created_at?: string;
}

interface AuthContextValue {
  user: User | null;
  token: string | null;
  isLoading: boolean;
  login: (email: string, password: string) => Promise<void>;
  signup: (username: string, email: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

// ── Token helpers ───────────────────────────────────────────────────────────

const TOKEN_KEY = "rag_auth_token";
const USER_KEY = "rag_auth_user";

function getStoredToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

function getStoredUser(): User | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(USER_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function storeAuth(token: string, user: User) {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(USER_KEY, JSON.stringify(user));
}

function clearAuth() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

// ── API helpers ─────────────────────────────────────────────────────────────

async function authFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(body.detail || `Auth error ${res.status}`);
  }
  return res.json() as Promise<T>;
}

// ── Provider ────────────────────────────────────────────────────────────────

const DEFAULT_USER: User = {
  id: "enterprise-admin",
  username: "Enterprise Admin",
  email: "admin@enterprise.ai",
};

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(DEFAULT_USER);
  const [token, setToken] = useState<string | null>("enterprise_session_active");
  const [isLoading, setIsLoading] = useState(false);

  const login = useCallback(async (_email: string, _password: string) => {
    setUser(DEFAULT_USER);
    setToken("enterprise_session_active");
  }, []);

  const signup = useCallback(
    async (_username: string, _email: string, _password: string) => {
      setUser(DEFAULT_USER);
      setToken("enterprise_session_active");
    },
    []
  );

  const logout = useCallback(() => {
    setUser(DEFAULT_USER);
  }, []);

  return (
    <AuthContext.Provider
      value={{ user, token, isLoading, login, signup, logout }}
    >
      {children}
    </AuthContext.Provider>
  );
}

// ── Hook ────────────────────────────────────────────────────────────────────

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return ctx;
}

/**
 * Returns the stored token for use in API headers.
 * Safe to call outside of React components (e.g., in api-client.ts).
 */
export function getAuthToken(): string | null {
  return getStoredToken();
}
