"use client";

import { useState, useEffect, useMemo } from "react";
import { ChevronDown, Loader2, Check, Database, Plus, AlertCircle, CheckCircle2, X, Search } from "lucide-react";
import { API_BASE } from "@/lib/api";
import { consumeSse } from "@/lib/sse";
import { useFetchStore } from "@/store/fetchStore";

interface TelegramChannel {
  telegram_id: number;
  title: string;
  username: string | null;
  type: string;
  in_database: boolean;
  db_id: number | null;
}

interface ChannelSelectorProps {
  onFetch?: (channels: number[]) => void;
}

interface Toast {
  id: number;
  type: "info" | "success" | "error";
  message: string;
}

export default function ChannelSelector({ onFetch }: ChannelSelectorProps) {
  const [channels, setChannels] = useState<TelegramChannel[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [isOpen, setIsOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [fetching, setFetching] = useState(false);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<{ type: "info" | "success" | "error"; message: string } | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);

  const beginRun = useFetchStore((s) => s.beginRun);
  const applyEvent = useFetchStore((s) => s.applyEvent);
  const endRun = useFetchStore((s) => s.endRun);
  const streamActive = useFetchStore((s) => s.active);

  useEffect(() => {
    loadTelegramChannels();
  }, []);

  const showToast = (type: "info" | "success" | "error", message: string) => {
    const id = Date.now();
    setToasts((prev) => [...prev, { id, type, message }]);
    const duration = type === "info" ? 3000 : 5000;
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), duration);
  };

  const removeToast = (id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  };

  const loadTelegramChannels = async (): Promise<TelegramChannel[]> => {
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/telegram-channels`);
      if (!res.ok) throw new Error(`API ${res.status}`);
      const data = await res.json();
      setChannels(data);
      return data;
    } catch (error) {
      console.error("Failed to load Telegram channels:", error);
      setStatus({ type: "error", message: "Failed to load Telegram channels" });
      return [];
    } finally {
      setLoading(false);
    }
  };

  const toggleChannel = (telegramId: number) => {
    const next = new Set(selected);
    if (next.has(telegramId)) {
      next.delete(telegramId);
    } else {
      next.add(telegramId);
    }
    setSelected(next);
    setStatus(null);
  };

  // Select-all / clear operate over the FILTERED list so search + bulk are
  // coherent: "Select all" after typing "degen" selects the matches.
  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return channels;
    return channels.filter((c) =>
      `${c.title} ${c.username || ""} ${c.type}`.toLowerCase().includes(q),
    );
  }, [channels, query]);

  const selectAll = () => {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const c of visible) next.add(c.telegram_id);
      return next;
    });
  };

  const clearAll = () => {
    setSelected(new Set());
    setStatus(null);
  };

  const handleFetch = async () => {
    if (selected.size === 0) return;

    // Keep dropdown open during entire fetch process
    setIsOpen(true);
    setFetching(true);

    try {
      // First, ensure all selected channels are in the database
      const toAdd = channels.filter(
        (c) => selected.has(c.telegram_id) && !c.in_database,
      );
      const addedMap = new Map<number, number>(); // telegram_id → new db_id

      if (toAdd.length > 0) {
        const message = `Adding ${toAdd.length} new channel${toAdd.length !== 1 ? "s" : ""} to database...`;
        setStatus({ type: "info", message });
        showToast("info", message);

        const addRes = await fetch(`${API_BASE}/api/add-channels`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            channels: toAdd.map((c) => ({
              telegram_id: c.telegram_id,
              title: c.title,
              username: c.username,
            })),
          }),
        });
        if (!addRes.ok) {
          throw new Error("Failed to add channels to database");
        }

        const addResult = await addRes.json();
        const addedCount = addResult.added?.filter((a: { status: string }) => a.status === "added").length || 0;
        const successMessage = `✓ Added ${addedCount} channel${addedCount !== 1 ? "s" : ""} to database`;
        setStatus({ type: "success", message: successMessage });
        showToast("success", successMessage);

        for (const a of addResult.added ?? []) {
          addedMap.set(a.telegram_id, a.db_id);
        }
      }

      // Resolve DB primary keys locally: existing channels from the current
      // list, newly added ones from the add response. Cards are keyed by the
      // SAME pk the server reports in every SSE event, so progress can never
      // land on the wrong card (the old stuck-"starting…" bug).
      const channelsToFetch = channels.filter((c) => selected.has(c.telegram_id));
      const channelIds = channelsToFetch.map(
        (c) => c.db_id ?? addedMap.get(c.telegram_id) ?? c.telegram_id,
      );

      // Queue every channel up front → grid shows Queued cards immediately;
      // the server runs them sequentially and flips each to Running/Done.
      beginRun(
        channelsToFetch.map((c, i) => ({
          channel_id: channelIds[i],
          title: c.title,
        })),
      );

      const response = await fetch(`${API_BASE}/api/fetch-stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // No days override: the server uses the standard 5-month window,
        // same anchor as the historical backfill preset.
        body: JSON.stringify({ channel_ids: channelIds }),
      });
      if (!response.ok) {
        throw new Error(`API ${response.status}`);
      }

      let successCount = 0;
      let errorCount = 0;
      await consumeSse(response, (data) => {
        if (data.status === "complete") return;
        applyEvent(data);
        if (data.status === "done") successCount++;
        else if (data.status === "error") errorCount++;
      });

      const finalMessage = `✓ Fetched ${successCount}/${channelIds.length} channel${channelIds.length !== 1 ? "s" : ""} (5-month window)`;
      setStatus({ type: errorCount > 0 ? "error" : "success", message: finalMessage });
      showToast(errorCount > 0 ? "error" : "success", finalMessage);

      // Refresh the dialog list so new channels show as "in database" next time
      loadTelegramChannels();

      // Notify parent to refresh the grid (clears finished cards)
      onFetch?.(channelIds);

      // Clear selection after successful fetch
      setSelected(new Set());
      setQuery("");

    } catch (error) {
      console.error("Fetch failed:", error);
      const errorMessage = `✗ Failed: ${error instanceof Error ? error.message : "Unknown error"}`;
      setStatus({ type: "error", message: errorMessage });
      showToast("error", errorMessage);
      endRun();
    } finally {
      setFetching(false);
      endRun();
    }
  };

  return (
    <div className="relative">
      {/* Toast Notifications */}
      <div className="fixed top-4 right-4 z-50 flex flex-col gap-2">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={`flex items-center gap-3 rounded-lg border p-4 shadow-lg backdrop-blur-sm animate-in slide-in-from-right ${
              toast.type === "error"
                ? "border-red-500/30 bg-red-500/10 text-red-400"
                : toast.type === "success"
                ? "border-green-500/30 bg-green-500/10 text-green-400"
                : "border-[var(--accent-teal)]/30 bg-[var(--accent-teal)]/10 text-[var(--accent-teal)]"
            }`}
          >
            {toast.type === "error" ? (
              <AlertCircle className="h-5 w-5 flex-shrink-0" />
            ) : toast.type === "success" ? (
              <CheckCircle2 className="h-5 w-5 flex-shrink-0" />
            ) : (
              <Loader2 className="h-5 w-5 flex-shrink-0 animate-spin" />
            )}
            <span className="text-sm font-medium">{toast.message}</span>
            <button
              onClick={() => removeToast(toast.id)}
              className="ml-2 flex-shrink-0 opacity-60 hover:opacity-100"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        ))}
      </div>

      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-card)] px-4 py-2 text-sm text-[var(--text-primary)] hover:border-[var(--border-hover)]"
      >
        <span>
          {selected.size === 0
            ? "Select channels"
            : `${selected.size} channel${selected.size > 1 ? "s" : ""} selected`}
        </span>
        <ChevronDown className={`h-4 w-4 transition-transform ${isOpen ? "rotate-180" : ""}`} />
      </button>

      {isOpen && (
        <>
          <div
            className="fixed inset-0 z-10"
            onClick={() => !fetching && setIsOpen(false)}
          />
          <div className="absolute left-0 top-full z-20 mt-2 w-96 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-card)] shadow-xl">
            <div className="border-b border-[var(--border-subtle)] p-3">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium text-[var(--text-primary)]">
                  Your Telegram Channels
                </span>
                <div className="flex gap-2">
                  <button
                    onClick={selectAll}
                    className="text-xs text-[var(--accent-teal)] hover:underline"
                    disabled={fetching}
                  >
                    Select all
                  </button>
                  <button
                    onClick={clearAll}
                    className="text-xs text-[var(--text-muted)] hover:underline"
                    disabled={fetching}
                  >
                    Clear
                  </button>
                </div>
              </div>
              {/* Search field */}
              <div className="relative mt-2">
                <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--text-muted)]" />
                <input
                  type="text"
                  placeholder="Search channels…"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  disabled={fetching}
                  className="w-full rounded-md border border-[var(--border-subtle)] bg-[var(--bg-primary)] py-1.5 pl-8 pr-7 text-xs text-[var(--text-primary)] placeholder-[var(--text-muted)] focus:border-[var(--accent-teal)] focus:outline-none"
                />
                {query && (
                  <button
                    onClick={() => setQuery("")}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-[var(--text-muted)] hover:text-[var(--text-primary)]"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
              {loading && (
                <div className="mt-2 flex items-center gap-2 text-xs text-[var(--text-muted)]">
                  <Loader2 className="h-3 w-3 animate-spin" />
                  Loading channels...
                </div>
              )}
            </div>

            {/* Status Message */}
            {status && (
              <div className={`mx-3 mt-3 flex items-start gap-2 rounded-lg border p-3 text-xs ${
                status.type === "error"
                  ? "border-red-500/30 bg-red-500/10 text-red-400"
                  : status.type === "success"
                  ? "border-green-500/30 bg-green-500/10 text-green-400"
                  : "border-[var(--accent-teal)]/30 bg-[var(--accent-teal)]/10 text-[var(--accent-teal)]"
              }`}>
                {status.type === "error" ? (
                  <AlertCircle className="h-4 w-4 flex-shrink-0 mt-0.5" />
                ) : status.type === "success" ? (
                  <CheckCircle2 className="h-4 w-4 flex-shrink-0 mt-0.5" />
                ) : (
                  <Loader2 className="h-4 w-4 flex-shrink-0 mt-0.5 animate-spin" />
                )}
                <span>{status.message}</span>
              </div>
            )}

            <div className="max-h-96 overflow-y-auto p-2">
              {visible.length === 0 && !loading && (
                <p className="px-3 py-6 text-center text-xs text-[var(--text-muted)]">
                  No channels match “{query}”.
                </p>
              )}
              {visible.map((channel) => (
                <button
                  key={channel.telegram_id}
                  onClick={() => toggleChannel(channel.telegram_id)}
                  disabled={fetching}
                  className="flex w-full items-center gap-3 rounded-md px-3 py-2 text-left hover:bg-[var(--bg-hover)] disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  <div
                    className={`flex h-5 w-5 items-center justify-center rounded border-2 ${
                      selected.has(channel.telegram_id)
                        ? "border-[var(--accent-teal)] bg-[var(--accent-teal)]"
                        : "border-[var(--border-subtle)]"
                    }`}
                  >
                    {selected.has(channel.telegram_id) && (
                      <Check className="h-3 w-3 text-white" />
                    )}
                  </div>
                  <div className="flex-1">
                    <div className="flex items-center gap-2">
                      <div className="text-sm text-[var(--text-primary)]">
                        {channel.title}
                      </div>
                      {channel.in_database ? (
                        <Database className="h-3 w-3 text-[var(--accent-teal)]" />
                      ) : (
                        <Plus className="h-3 w-3 text-[var(--accent-gold)]" />
                      )}
                    </div>
                    <div className="text-xs text-[var(--text-muted)]">
                      {channel.username ? `@${channel.username}` : channel.type}
                    </div>
                  </div>
                </button>
              ))}
            </div>

            <div className="border-t border-[var(--border-subtle)] p-3">
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  handleFetch();
                }}
                disabled={selected.size === 0 || fetching || streamActive}
                className="flex w-full items-center justify-center gap-2 rounded-lg bg-[var(--accent-teal)] px-4 py-2 text-sm font-medium text-white disabled:opacity-50 disabled:cursor-not-allowed hover:bg-[var(--accent-teal)]/90"
              >
                {fetching ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Fetching data… watch the cards below
                  </>
                ) : (
                  `Fetch ${selected.size} channel${selected.size !== 1 ? "s" : ""}`
                )}
              </button>
              {status && status.type === "success" && (
                <button
                  onClick={() => {
                    setStatus(null);
                    setIsOpen(false);
                  }}
                  className="mt-2 w-full rounded-lg border border-[var(--border-subtle)] px-4 py-1.5 text-xs text-[var(--text-secondary)] hover:bg-[var(--bg-hover)]"
                >
                  Close
                </button>
              )}
              <div className="mt-2 flex items-center gap-4 text-xs text-[var(--text-muted)]">
                <div className="flex items-center gap-1">
                  <Database className="h-3 w-3 text-[var(--accent-teal)]" />
                  <span>In database</span>
                </div>
                <div className="flex items-center gap-1">
                  <Plus className="h-3 w-3 text-[var(--accent-gold)]" />
                  <span>New (will be added)</span>
                </div>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
