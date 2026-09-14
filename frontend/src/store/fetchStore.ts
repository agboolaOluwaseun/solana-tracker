"use client";

/**
 * Shared state for the add-channel / refresh pipeline runs.
 *
 * ONE store (not two useState copies) so the loading cards in the grid and
 * the status inside the ChannelSelector can never disagree or key off
 * different ids — the old bug where the selector tracked the Telegram id
 * while the server reported the DB primary key (card stuck at "starting…"
 * + a real 0-call card appearing beside it) is impossible now: everything
 * keys off the id the SERVER reports in its events.
 *
 * The SSE endpoints run channels sequentially; while channel N is running
 * the rest wait. Queued channels get a card immediately (status queued) so
 * you see the whole queue the moment you click Fetch.
 */
import { create } from "zustand";
import type { SseEvent } from "@/lib/sse";

export type FetchStatus = "queued" | "running" | "done" | "error";

export interface FetchTask {
  /** DB primary key when known; before the server's first event we key by
   * the id the request was made with and re-key on the server's `start`. */
  channel_id: number;
  title: string;
  status: FetchStatus;
  stage: string; // 'fetch' | 'parse' | 'price' | 'done' | 'error'
  scanned: number;
  found: number;
  priced: number;
  unpriceable: number;
  live: number;
  waiting: number;
  total_calls: number;
  /** Rolling narration lines — what just happened, real counters only. */
  log: string[];
  /** Last message appended (dedupe consecutive identical stage lines). */
  lastMessage: string;
}

interface FetchState {
  tasks: FetchTask[];
  active: boolean; // a stream is open
  /** Queue up N channels before the stream starts; all get 'queued' cards. */
  beginRun: (items: { channel_id: number; title: string }[]) => void;
  /** Apply one SSE event payload (queue | start | progress | done | error | complete). */
  applyEvent: (data: SseEvent) => void;
  endRun: () => void;
  /** Remove done/error cards once the grid has reloaded and shows real cards. */
  clearFinished: () => void;
}

const MAX_LOG = 12;

function pushLine(t: FetchTask, msg: string): FetchTask {
  if (!msg || msg === t.lastMessage) return { ...t, lastMessage: msg };
  const log = [...t.log, msg];
  return { ...t, log: log.slice(-MAX_LOG), lastMessage: msg };
}

export const useFetchStore = create<FetchState>((set) => ({
  tasks: [],
  active: false,

  beginRun: (items) =>
    set({
      active: true,
      tasks: items.map((it) => ({
        channel_id: it.channel_id,
        title: it.title,
        status: "queued",
        stage: "",
        scanned: 0,
        found: 0,
        priced: 0,
        unpriceable: 0,
        live: 0,
        waiting: 0,
        total_calls: 0,
        log: [],
        lastMessage: "",
      })),
    }),

  applyEvent: (data) =>
    set((state) => {
      const tasks = [...state.tasks];
      // 'complete' is a stream-level event (no channel_id) — ignore here.
      if (!data.channel_id) return state;

      // Re-key: the client initially queues by whatever id it had (could be a
      // Telegram id for freshly-added channels); the server's events carry
      // the authoritative DB primary key. Match by position of the first
      // queued/running task when ids don't line up, then stamp the real id.
      let idx = tasks.findIndex((t) => t.channel_id === data.channel_id);
      if (idx === -1 && (data.status === "start" || data.status === "progress")) {
        const firstOpen = tasks.findIndex(
          (t) => t.status === "queued" || t.status === "running",
        );
        if (firstOpen !== -1) {
          tasks[firstOpen] = { ...tasks[firstOpen], channel_id: data.channel_id };
          idx = firstOpen;
        }
      }
      if (idx === -1) return state;

      let t = { ...tasks[idx] };
      if (data.title) t.title = data.title;

      switch (data.status) {
        case "start":
          t = pushLine(
            { ...t, status: "running", stage: "fetch" },
            "starting…",
          );
          break;
        case "progress": {
          t = { ...t, status: "running" };
          if (data.stage) t.stage = data.stage;
          if (typeof data.scanned === "number") t.scanned = data.scanned;
          if (typeof data.found === "number") t.found = data.found;
          if (typeof data.priced === "number") t.priced = data.priced;
          if (typeof data.unpriceable === "number") t.unpriceable = data.unpriceable;
          if (typeof data.live === "number") t.live = data.live;
          if (typeof data.waiting === "number") t.waiting = data.waiting;
          if (typeof data.total_calls === "number") t.total_calls = data.total_calls;
          if (data.message) t = pushLine(t, data.message);
          break;
        }
        case "done":
          t = pushLine(
            { ...t, status: "done", stage: "done" },
            `finished — ${t.priced} priced, ${t.unpriceable} unpriceable`,
          );
          break;
        case "error":
          t = pushLine(
            { ...t, status: "error", stage: "error" },
            `✗ ${data.message || "failed"}`,
          );
          break;
      }
      tasks[idx] = t;
      return { tasks };
    }),

  endRun: () => set({ active: false }),

  clearFinished: () =>
    set((state) => ({
      tasks: state.tasks.filter((t) => t.status !== "done" && t.status !== "error"),
    })),
}));

/** True while any task is still queued/running (grid should keep finished
 * cards visible so the sequential flow is readable until the run ends). */
export function hasOpenTasks(tasks: FetchTask[]): boolean {
  return tasks.some((t) => t.status === "queued" || t.status === "running");
}
