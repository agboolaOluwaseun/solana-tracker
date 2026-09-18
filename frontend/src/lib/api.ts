/**
 * Thin client for the local Python API (http://127.0.0.1:8000).
 * All endpoints accept ?strategy=normal|stoploss and ?window=1d|7d|1m|3m|all.
 */
const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8000";
export { API_BASE };

export interface ApiChannel {
  channel_id: number;
  username: string | null;
  title: string;
  chain?: string;
  chains?: string[] | null;  // only set when ?chain=all (which chains have decided calls)
  total_calls: number;
  win_rate: number | null;
  avg_peak_profit_pct: number | null;
  streak: number;
  created_at: string | null;
}

export interface ApiLeaderboardRow {
  rank: number;
  channel_id: number;
  channel_title: string;
  channel_username: string | null;
  chain?: string;
  chains?: string[] | null;
  total_calls: number;
  wins: number;
  win_rate: number | null;
  avg_peak_profit_pct: number | null;
  top_call_roi: number | null;
  top_call_token: string | null;
}

export interface ApiDetail {
  id: number;
  username: string | null;
  title: string;
  chain?: string;
  total_calls: number;
  wins: number;
  win_rate: number | null;
  avg_peak_profit_pct: number | null;
}

export interface ApiBucket {
  bucket: string;
  total_calls: number;
  wins: number;
  win_rate: number | null;
}

export interface ApiCall {
  id: number;
  token_address: string;
  token_symbol: string | null;
  token_name: string | null;
  call_timestamp: string;
  entry_price_usd: number | null;
  peak_price_usd: number | null;
  peak_profit_pct: number | null;
  is_win: number;
  status: string;
  multiplier: number | null;
}

export interface ApiTokenCallEntry {
  call_id: number;
  channel_title: string;
  channel_username: string | null;
  entry_price_usd: number | null;
  peak_price_usd: number | null;
  call_timestamp: string;
  is_win: boolean;
  multiplier: number | null;
}

export interface ApiToken {
  token_address: string;
  token_symbol: string;
  token_name: string | null;
  channels_count: number;
  calls_count: number;
  call_entries: ApiTokenCallEntry[];
}

export interface ApiTier {
  tier: string;   // "100x" ... "2x", "<2x"
  count: number;
  pct: number;
}

export interface ApiTiersResponse {
  scope: { chain: string; strategy: string; window: string; days: number | null; since: string | null };
  total_decided: number;
  wins: number;
  win_rate: number | null;
  tiers: ApiTier[];
  granular_calls: number | null;
  avg_api_requests: number | null;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`API ${res.status}: ${path}`);
  return res.json();
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`API ${res.status}: ${path}`);
  return res.json();
}

export const api = {
  channels: (strategy: string, window: string, chain: string) =>
    get<ApiChannel[]>(`/api/channels?strategy=${strategy}&window=${window}&chain=${chain}`),
  leaderboard: (strategy: string, window: string, chain: string) =>
    get<ApiLeaderboardRow[]>(`/api/leaderboard?strategy=${strategy}&window=${window}&chain=${chain}`),
  tokens: (window: string, chain: string) =>
    get<ApiToken[]>(`/api/tokens?window=${window}&chain=${chain}`),
  detail: (handle: string, strategy: string, window: string, chain: string) =>
    get<ApiDetail>(`/api/channels/${encodeURIComponent(handle)}?strategy=${strategy}&window=${window}&chain=${chain}`),
  buckets: (handle: string, strategy: string, window: string, chain: string) =>
    get<ApiBucket[]>(`/api/channels/${encodeURIComponent(handle)}/buckets?strategy=${strategy}&window=${window}&chain=${chain}`),
  streak: (handle: string, strategy: string, chain: string) =>
    get<{ streak: number }>(`/api/channels/${encodeURIComponent(handle)}/streak?strategy=${strategy}&chain=${chain}`),
  calls: (handle: string, window: string, chain: string) =>
    get<ApiCall[]>(`/api/channels/${encodeURIComponent(handle)}/calls?window=${window}&chain=${chain}`),
  tiers: (handle: string, chain: string, strategy: string, days: number | null, window: string) =>
    get<ApiTiersResponse>(
      `/api/channels/${encodeURIComponent(handle)}/tiers?chain=${chain}&strategy=${strategy}` +
      (days ? `&days=${days}` : `&window=${window}`),
    ),
  fetch: (channelIds: number[], days?: number) =>
    post<{ success: boolean; results: Array<{ channel_id: number; success: boolean; message: string }> }>("/api/fetch", { channel_ids: channelIds, ...(days ? { days } : {}) }),
};

/** AI chat envelope. success:false + error kind is an EXPECTED outcome
 * (no key, expired sub, rate limit) — never throw, the page renders it. */
export interface AiChatResponse {
  success: boolean;
  reply?: string;
  model?: string;
  error?: string;
  message?: string;
}

export async function aiChat(
  messages: { role: "user" | "assistant"; content: string }[],
): Promise<AiChatResponse> {
  try {
    return await post<AiChatResponse>("/api/ai-chat", { messages });
  } catch (e) {
    // network down / server offline — degrade to the same banner channel
    return {
      success: false,
      error: "offline",
      message: `Can't reach the local API server. Is uvicorn running? (${e instanceof Error ? e.message : "unknown error"})`,
    };
  }
}
/** Map store strategy ("50"|"100") to API strategy ("stoploss"|"normal"). */
export function apiStrategy(s: string): string {
  return s === "50" ? "stoploss" : "normal";
}

/** Deterministic initials avatar (data-URI SVG) from a title/seed. */
export function initialsAvatar(title: string): string {
  const initials = (title || "?")
    .split(/\s+/)
    .map((w) => w[0])
    .slice(0, 2)
    .join("")
    .toUpperCase();
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='96' height='96'><rect width='96' height='96' rx='48' fill='%2300C19F'/><text x='48' y='60' font-size='36' text-anchor='middle' fill='%230a0a0a' font-family='sans-serif' font-weight='bold'>${initials}</text></svg>`;
  return `data:image/svg+xml,${svg}`;
}
