// ── UI / derived types ──
// Domain data shapes for API responses live in @/lib/api (ApiChannel,
// ApiLeaderboardRow, ApiToken, ...). Only UI-facing types live here.

export type TimeWindow = "1d" | "7d" | "1m" | "3m" | "all";
export type Strategy = "50" | "100";  // 50 = stop-loss strategy, 100 = normal (2x)
export type Chain = "sol" | "robinhood" | "eth" | "bsc" | "base" | "arc" | "all";
export type ChannelTab = "All" | "Hot" | "Consistent" | "New";

export interface ChannelCardData {
  channel_id: number;
  title: string;
  username: string | null;
  avatar_url: string;
  chain?: string;
  chains?: string[] | null;  // per-chain tags when global chain === "all"
  total_calls: number;
  win_rate: number;
  avg_multiplier: number;
  streak: number;
  verified?: boolean;
  created_at?: string;  // ISO8601 — for "New" tab sorting
}

export interface CallModalData {
  channel_title: string;
  channel_avatar: string;
  token_address: string;
  message_text: string;
  timestamp: string;
  telegram_url?: string;
}
