"use client";

import { FormEvent, useEffect, useState } from "react";
import { Lock, Loader2 } from "lucide-react";
import {
  clearAccessToken,
  getAccessToken,
  getAuthStatus,
  setAccessToken,
  verifyAccessPassword,
} from "@/lib/auth";

export default function AuthGate({ children }: { children: React.ReactNode }) {
  const [checking, setChecking] = useState(true);
  const [enabled, setEnabled] = useState(false);
  const [unlocked, setUnlocked] = useState(false);
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;

    async function checkAuth() {
      try {
        const status = await getAuthStatus();
        if (cancelled) return;

        setEnabled(status.enabled);
        if (!status.enabled) {
          setUnlocked(true);
          return;
        }

        const saved = getAccessToken();
        if (saved && await verifyAccessPassword(saved)) {
          setUnlocked(true);
        } else {
          clearAccessToken();
        }
      } catch {
        if (!cancelled) {
          setEnabled(true);
          clearAccessToken();
        }
      } finally {
        if (!cancelled) setChecking(false);
      }
    }

    checkAuth();
    return () => { cancelled = true; };
  }, []);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = password.trim();
    if (!value || submitting) return;

    setSubmitting(true);
    setError("");
    try {
      if (await verifyAccessPassword(value)) {
        setAccessToken(value);
        setUnlocked(true);
      } else {
        setError("密码不正确");
      }
    } catch {
      setError("无法连接到服务");
    } finally {
      setSubmitting(false);
    }
  }

  if (checking) {
    return (
      <div className="h-full flex items-center justify-center bg-gray-50 text-gray-500">
        <Loader2 className="w-5 h-5 animate-spin" />
      </div>
    );
  }

  if (!enabled || unlocked) {
    return <>{children}</>;
  }

  return (
    <div className="min-h-full flex items-center justify-center bg-gray-50 px-4">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-sm bg-white border border-gray-200 rounded-lg shadow-sm p-6"
      >
        <div className="flex items-center gap-3 mb-5">
          <div className="w-10 h-10 rounded-lg bg-gray-900 text-white flex items-center justify-center">
            <Lock className="w-5 h-5" />
          </div>
          <div>
            <h1 className="text-base font-semibold text-gray-900">访问 Multi-Agent Studio</h1>
            <p className="text-sm text-gray-500 mt-0.5">请输入访问密码</p>
          </div>
        </div>

        <input
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          autoFocus
          className="w-full h-10 rounded-lg border border-gray-300 px-3 text-sm outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
        />

        {error && <p className="mt-3 text-sm text-red-600">{error}</p>}

        <button
          type="submit"
          disabled={submitting || !password.trim()}
          className="mt-5 w-full h-10 rounded-lg bg-gray-900 text-white text-sm font-medium hover:bg-gray-800 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
        >
          {submitting && <Loader2 className="w-4 h-4 animate-spin" />}
          进入
        </button>
      </form>
    </div>
  );
}
