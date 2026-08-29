"use client";

import { useState, useEffect } from "react";
import { ChevronDown, Loader2, Check, Database, Plus, AlertCircle, CheckCircle2, X } from "lucide-react";

interface TelegramChannel {
  telegram_id: number;
  title: string;
  username: string | null;
  type: string;
  in_database: boolean;
  db_id: number | null;
}

interface FetchingChannel {
  channel_id: number;
  title: string;
  stage: string;
  scanned: number;
  found: number;
  total_calls: number;
}

interface ChannelSelectorProps {
  onFetch?: (channels: number[]) => void;
  onFetchingChange?: (fetching: FetchingChannel[]) => void;
}

interface Toast {
  id: number;
  type: "info" | "success" | "error";
  message: string;
}

export default function ChannelSelector({ onFetch, onFetchingChange }: ChannelSelectorProps) {
  const [channels, setChannels] = useState<TelegramChannel[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [isOpen, setIsOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [fetching, setFetching] = useState(false);
  const [, setFetchingChannels] = useState<FetchingChannel[]>([]);
  const [status, setStatus] = useState<{ type: "info" | "success" | "error"; message: string } | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);

  useEffect(() => {
    loadTelegramChannels();
  }, []);

  const showToast = (type: "info" | "success" | "error", message: string) => {
    const id = Date.now();
    setToasts((prev) => [...prev, { id, type, message }]);
    
    // Auto-remove after 5 seconds for success/error, 3 seconds for info
    const duration = type === "info" ? 3000 : 5000;
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, duration);
  };

  const removeToast = (id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  };

  const loadTelegramChannels = async (): Promise<TelegramChannel[]> => {
    setLoading(true);
    try {
      const res = await fetch("http://127.0.0.1:8000/api/telegram-channels");
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
    setStatus(null); // Clear status when selection changes
  };

  const selectAll = () => {
    setSelected(new Set(channels.map((c) => c.telegram_id)));
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
        (c) => selected.has(c.telegram_id) && !c.in_database
      );
      const addedMap = new Map<number, number>(); // telegram_id → new db_id
      
      if (toAdd.length > 0) {
        const message = `Adding ${toAdd.length} new channel${toAdd.length !== 1 ? "s" : ""} to database...`;
        setStatus({ type: "info", message });
        showToast("info", message);
        
        const addRes = await fetch("http://127.0.0.1:8000/api/add-channels", {
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
        
        // Map telegram_id → new db_id from the add response (no extra round-trip)
        for (const a of addResult.added ?? []) {
          addedMap.set(a.telegram_id, a.db_id);
        }
      }

      // Resolve db_ids locally: existing channels from current state,
      // newly added ones from the add response. No mid-fetch refresh,
      // so the new channel can't appear in the grid twice.
      const channelsToFetch = channels.filter((c) => selected.has(c.telegram_id));
      const channelIds = channelsToFetch.map(
        (c) => c.db_id ?? addedMap.get(c.telegram_id) ?? c.telegram_id
      );
      
      // Initialize fetching state
      const initialFetching: FetchingChannel[] = channelsToFetch.map((c) => ({
        channel_id: c.db_id || c.telegram_id,
        title: c.title,
        stage: "start",
        scanned: 0,
        found: 0,
        total_calls: 0,
      }));
      setFetchingChannels(initialFetching);
      onFetchingChange?.(initialFetching);
      
      // Use streaming endpoint
      const response = await fetch("http://127.0.0.1:8000/api/fetch-stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channel_ids: channelIds, days: 7 }),
      });
      
      if (!response.ok) {
        throw new Error(`API ${response.status}`);
      }
      
      const reader = response.body?.getReader();
      const decoder = new TextDecoder();
      let successCount = 0;
      let errorCount = 0;
      
      if (!reader) throw new Error("No response body");
      
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        
        const text = decoder.decode(value);
        const lines = text.split("\n").filter((line) => line.startsWith("data: "));
        
        for (const line of lines) {
          const data = JSON.parse(line.substring(6));
          
          if (data.status === "complete") {
            continue;
          }
          
          // Update fetching state. NOTE: data.stage is only used for real
          // pipeline stages; the terminal 'done' event must NOT overwrite
          // stage (it would flash a fake 100% before the card unmounts).
          setFetchingChannels((prev) => {
            const updated = prev.map((ch) => {
              if (ch.channel_id === data.channel_id) {
                return {
                  ...ch,
                  stage: data.stage ?? ch.stage,
                  scanned: data.scanned ?? ch.scanned,
                  found: data.found ?? ch.found,
                  total_calls: data.total_calls ?? ch.total_calls,
                };
              }
              return ch;
            });
            onFetchingChange?.(updated);
            return updated;
          });
          
          if (data.status === "done") {
            successCount++;
          } else if (data.status === "error") {
            errorCount++;
          }
        }
      }
      
      const finalMessage = `✓ Successfully fetched data for ${successCount}/${channelIds.length} channel${channelIds.length !== 1 ? "s" : ""} (last 7 days)`;
      setStatus({ 
        type: errorCount > 0 ? "error" : "success", 
        message: finalMessage 
      });
      showToast(errorCount > 0 ? "error" : "success", finalMessage);
      
      // Clear fetching state
      setFetchingChannels([]);
      onFetchingChange?.([]);
      
      // Refresh the dialog list so new channels show as "in database" next time
      loadTelegramChannels();
      
      // Notify parent to refresh
      onFetch?.(channelIds);
      
      // Clear selection after successful fetch
      setSelected(new Set());
      
    } catch (error) {
      console.error("Fetch failed:", error);
      const errorMessage = `✗ Failed: ${error instanceof Error ? error.message : "Unknown error"}`;
      setStatus({ 
        type: "error", 
        message: errorMessage 
      });
      showToast("error", errorMessage);
    } finally {
      setFetching(false);
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
              {channels.map((channel) => (
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
                disabled={selected.size === 0 || fetching}
                className="flex w-full items-center justify-center gap-2 rounded-lg bg-[var(--accent-teal)] px-4 py-2 text-sm font-medium text-white disabled:opacity-50 disabled:cursor-not-allowed hover:bg-[var(--accent-teal)]/90"
              >
                {fetching ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Fetching data...
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
