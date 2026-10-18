// REST commands. Every mutating call goes through the engine's ControlPlane (audited).

const TOKEN_KEY = "pmtrader.token";

export function getToken(): string | null {
  const fromUrl = new URLSearchParams(location.search).get("token");
  try {
    if (fromUrl) localStorage.setItem(TOKEN_KEY, fromUrl);
    return fromUrl ?? localStorage.getItem(TOKEN_KEY);
  } catch {
    return fromUrl;
  }
}

export class ApiError extends Error {}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(data.detail ?? `${res.status} ${res.statusText}`);
  return data as T;
}

export const api = {
  pause: (reason = "paused from web") => call<{ result: string }>("POST", "/api/control/pause", { reason }),
  resume: (confirm?: string) => call<{ result: string }>("POST", "/api/control/resume", { confirm }),
  kill: (flatten = false) =>
    call<{ result: string }>("POST", "/api/control/kill", { reason: "kill switch (web)", flatten }),
  flatten: () => call<{ result: string }>("POST", "/api/control/flatten"),
  cancelAll: () => call<{ cancelled: number }>("POST", "/api/control/cancel_all"),
  cancelOrder: (id: string) => call<{ cancelled: boolean }>("DELETE", `/api/orders/${id}`),
  setLimits: (changes: Record<string, number>) => call<{ applied: Record<string, number> }>("PUT", "/api/limits", { changes }),
  setStrategyAuto: (name: string, auto: boolean) => call("PUT", `/api/strategies/${name}`, { auto }),
  placeOrder: (o: {
    market_id: string;
    asset_id: string;
    side: "BUY" | "SELL";
    price: number;
    size: number;
    order_type: "GTC" | "FAK";
    post_only: boolean;
  }) => call<{ ok: boolean; code: string; message: string; status: string | null }>("POST", "/api/orders", o),
  closePosition: (assetId: string) =>
    call<{ ok: boolean; code: string; message: string }>("POST", `/api/positions/${assetId}/close`, {}),
  executeOpportunity: (key: string) =>
    call<{ ok: boolean; code: string; message: string }>("POST", `/api/opportunities/execute?key=${encodeURIComponent(key)}`),
  searchMarkets: (q: string) =>
    call<{ id: string; question: string; volume_24h: number; live: boolean; outcomes: string[] }[]>(
      "GET",
      `/api/markets?q=${encodeURIComponent(q)}`,
    ),
  market: (id: string) =>
    call<{
      id: string;
      question: string;
      tick: number;
      min_size: number;
      url: string;
      outcomes: { name: string; asset_id: string }[];
      books: Record<string, { outcome: string; bids: [number, number][]; asks: [number, number][] }>;
    }>("GET", `/api/market/${id}`),
};
