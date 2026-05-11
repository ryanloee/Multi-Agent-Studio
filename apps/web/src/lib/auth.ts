const ACCESS_TOKEN_KEY = "mas_access_token";
const API_BASE = process.env.NEXT_PUBLIC_API_URL || "/api";

export function getAccessToken(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(ACCESS_TOKEN_KEY) || "";
}

export function setAccessToken(token: string): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(ACCESS_TOKEN_KEY, token);
}

export function clearAccessToken(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(ACCESS_TOKEN_KEY);
}

export function authHeaders(): Record<string, string> {
  const token = getAccessToken();
  return token ? { "X-MAS-Access-Token": token } : {};
}

export async function getAuthStatus(): Promise<{ enabled: boolean }> {
  const response = await fetch(`${API_BASE}/auth/status`, {
    headers: authHeaders(),
  });
  if (!response.ok) {
    return { enabled: true };
  }
  return response.json();
}

export async function verifyAccessPassword(password: string): Promise<boolean> {
  const response = await fetch(`${API_BASE}/auth/verify`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-MAS-Access-Token": password,
    },
    body: JSON.stringify({ password }),
  });
  return response.ok;
}

export function withAccessToken(url: string): string {
  const token = getAccessToken();
  if (!token) return url;

  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}access_token=${encodeURIComponent(token)}`;
}
