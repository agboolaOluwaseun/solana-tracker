/**
 * Shared fetch state + stream orchestration — one store read by BOTH the
 * "Fetch channels" dropdown (ChannelSelector) and the channel grid cards /
 * bottom update feed (page.tsx), so the same in-flight run can never render
 * twice with different progress.
 *
 * The old bug this fixes: ChannelSelector kept a LOCAL tasks state keyed by
 * the Telegram channel id (what the dropdown knows), while page.tsx created
 * a second state keyed by the server event's DB primary key — two cards per
 * channel whose progress updated inconsistently. Now there is ONE keyed list;
 * the server's `start` event (DB pk) re-keys a queued card in place.
 *
 * Run kinds:
 *   fetch   — user picks channels in the dropdown (5-month backfill);
 *             progress renders in the grid cards.
 *   refresh — boot auto-update + "Refresh All" (delta since last call);
 *             progress renders in the bottom "Updating channels" feed.
 *
 * Contention policy (user request, 2026-09-15): if a boot refresh is running
 * and the user starts a dropdown fetch, the refresh is ABORTED AT A CHANNEL
 * BOUNDARY (never mid-channel — a half-priced channel would strand rows in
 * 'running'), the fetch takes over, and the refresh RESTARTS automatically
 * when the fetch finishes. Each run owns its own Telethon client on its own
 * asyncio loop, but two simultaneous Telegram streams share one session file
 * and invite FloodWait/races — one-at-a-time is deliberate.
 */
import { create } from "zustand";
import type { SseEvent } from "@/lib/sse";
import { consumeSse } from "@/lib/sse";
import { API_BASE } from "@/lib/api";

export type FetchStatus = "queued" | "running" | "done" | "error";
export type RunKind = "fetch" | "refresh";

/** One narration line in the bottom "Updating channels" feed panel. */
export interface FeedLine {
  ch: string;
  line: string;
}

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
  /** What the current run is: fetch (grid cards) or refresh (bottom feed). */
  kind: RunKind | null;
  /** Rolling global narration for the bottom panel (refresh runs only). */
  feed: FeedLine[];
  /** Set once a refresh stream ended cleanly — panel says all up to date. */
  allDone: boolean;
  /** Seed the task list + panel header for a new run (empty items = wait for
   * the server's 'queue' event). */
  beginRun: (items: { channel_id: number; title: string }[], kind: RunKind) => void;
  /** Apply one SSE event payload (queue | start | progress | done | error |
   * rescore_start | rescore_done | complete). */
  applyEvent: (data: SseEvent) => void;
  /** Stream ended (any kind): flips allDone when every refresh task finished. */
  endRun: () => void;
  /** Remove done/error cards once the grid has reloaded and shows real cards. */
  clearFinished: () => void;
  /** Hide the bottom panel / reset refresh state. */
  clearFeed: () => void;
}

const MAX_LOG = 12;
const MAX_FEED = 80;

function blankTask(it: { channel_id: number; title: string }): FetchTask {
  return {
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
  };
}

function pushLine(t: FetchTask, msg: string): FetchTask {
  if (!msg || msg === t.lastMessage) return { ...t, lastMessage: msg };
  const log = [...t.log, msg];
  return { ...t, log: log.slice(-MAX_LOG), lastMessage: msg };
}

/* ── stream orchestration (module-scoped; not renderable state) ─────────── */

let controller: AbortController | null = null;
let refreshRunning = false; // guards double-start (mount vs button)
let resumePending = false; // refresh was yielded to a fetch → restart after

export function streamActive(): boolean {
  return controller !== null;
}

/** Core: open a stream, dispatch events into the store, always endRun. */
async function runStream(
  kind: RunKind,
  path: string,
  body: unknown,
  items: { channel_id: number; title: string }[],
): Promise<void> {
  const s = useFetchStore.getState();
  controller?.abort(); // one stream at a time
  const myController = new AbortController();
  controller = myController;
  if (kind === "refresh") refreshRunning = true;
  s.beginRun(items, kind);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
      signal: myController.signal,
    });
    if (!response.ok) throw new Error(`API ${response.status}`);
    await consumeSse(response, (data) => {
      if (data.status === "complete") return;
      useFetchStore.getState().applyEvent(data);
    });
  } catch (err) {
    if (kind === "refresh" && controller === myController) {
      useFetchStore.setState((s) => ({
        feed: [
          ...s.feed,
          { ch: "—", line: `✗ update failed: ${err instanceof Error ? err.message : "network error"}` },
        ].slice(-MAX_FEED),
      }));
    }
    throw err;
  } finally {
    const owned = controller === myController; // a newer run hasn't taken over
    if (owned) controller = null;
    if (kind === "refresh") refreshRunning = false;
    if (owned) useFetchStore.getState().endRun();
    if (!owned) return; // a fetch replaced us mid-flight; it owns the resume
    if (kind === "fetch" && resumePending) {
      // The fetch the refresh yielded to is finished — resume the background
      // update of everything the abort skipped.
      resumePending = false;
      setTimeout(() => {
        void runStream("refresh", "/api/refresh-stream", {}, []);
      }, 400);
    }
  }
}

/** Boot auto-update + "Refresh All". Returns false if a refresh was already
 * running (the caller must not claim a fresh completion it didn't produce). */
export async function runRefreshStream(): Promise<boolean> {
  if (refreshRunning) return false;
  await runStream("refresh", "/api/refresh-stream", {}, []);
  return true;
}

/** Dropdown fetch. Yields the background refresh to this run (abort at the
 * channel boundary, auto-restart when done). */
export async function runFetchStream(
  channelIds: number[],
  items: { channel_id: number; title: string }[],
): Promise<void> {
  if (controller) {
    resumePending = true; // restart refresh after this fetch
  }
  await runStream("fetch", "/api/fetch-stream", { channel_ids: channelIds }, items);
}

/** User dismissed the panel mid-refresh: stop the stream, no resume. */
export function stopRefresh() {
  resumePending = false;
  controller?.abort();
  controller = null;
}

export const useFetchStore = create<FetchState>((set, get) => ({
  tasks: [],
  active: false,
  kind: null,
  feed: [],
  allDone: false,

  beginRun: (items, kind) =>
    set((state) => ({
      active: true,
      kind,
      allDone: false,
      // Empty items = wait for the server's 'queue' event. For refresh runs,
      // clear any finished FETCH cards first so the panel's n/total counter
      // never renders stale rows for a moment (e.g. on refresh auto-resume
      // after a fetch yielded the stream).
      tasks: items.length
        ? items.map(blankTask)
        : kind === "refresh"
          ? []
          : state.tasks,
      feed:
        kind === "refresh" && items.length
          ? [
              {
                ch: "—",
                line: `updating ${items.length} channel${items.length === 1 ? "" : "s"}…`,
              },
            ]
          : kind === "refresh"
            ? state.feed
            : [],
    })),

  applyEvent: (data) =>
    set((state) => {
      // Stream-level rescore narration (refresh runs only).
      if (data.status === "rescore_start" || data.status === "rescore_done") {
        if (state.kind !== "refresh") return state;
        const line =
          data.status === "rescore_start"
            ? "refreshing live verdicts (calls younger than 7 days)…"
            : `live verdicts refreshed — ${data.updated ?? 0} calls updated`;
        return { ...state, feed: [...state.feed, { ch: "—", line }].slice(-MAX_FEED) };
      }
      // 'queue' carries the whole channel list (no channel_id) — seed tasks.
      if (data.status === "queue" && data.channels) {
        const fresh = data.channels.map(blankTask);
        const merged = fresh.map(
          (f) => state.tasks.find((t) => t.channel_id === f.channel_id) ?? f,
        );
        const line = `updating ${fresh.length} channel${fresh.length === 1 ? "" : "s"}…`;
        return {
          ...state,
          active: true,
          tasks: merged,
          allDone: false,
          feed:
            state.kind === "refresh"
              ? [...state.feed, { ch: "—", line }].slice(-MAX_FEED)
              : state.feed,
        };
      }
      if (!data.channel_id) return state;

      // Re-key: the client initially queues by whatever id it had (could be a
      // Telegram id for freshly-added channels); the server's events carry
      // the authoritative DB primary key. Match by position of the first
      // queued/running task when ids don't line up, then stamp the real id.
      let idx = state.tasks.findIndex((t) => t.channel_id === data.channel_id);
      if (idx === -1 && (data.status === "start" || data.status === "progress")) {
        const firstOpen = state.tasks.findIndex(
          (t) => t.status === "queued" || t.status === "running",
        );
        if (firstOpen !== -1) {
          const pre = [...state.tasks];
          pre[firstOpen] = { ...pre[firstOpen], channel_id: data.channel_id! };
          state = { ...state, tasks: pre };
          idx = firstOpen;
        }
      }
      if (idx === -1) return state;

      const tasks = [...state.tasks];
      let t = { ...tasks[idx] };
      if (data.title) t.title = data.title;

      // Bottom-panel narration: refresh runs mirror every channel-tagged
      // progress line (fetch runs stay card-only, as designed).
      let feed = state.feed;
      const pushFeed = (line: string) => {
        const last = feed[feed.length - 1];
        if (last && last.ch === t.title && last.line === line) return;
        feed = [...feed, { ch: t.title, line }].slice(-MAX_FEED);
      };

      switch (data.status) {
        case "start":
          t = pushLine({ ...t, status: "running", stage: "fetch" }, "starting…");
          if (state.kind === "refresh") pushFeed("starting…");
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
          if (data.message) {
            t = pushLine(t, data.message);
            if (state.kind === "refresh") pushFeed(data.message);
          }
          break;
        }
        case "done": {
          const summary =
            data.message ||
            (t.total_calls === 0
              ? "up to date"
              : `done — ${t.priced + t.live} priced, ${t.unpriceable} unpriceable${
                  t.waiting ? `, ${t.waiting} waiting` : ""
                }`);
          t = pushLine({ ...t, status: "done", stage: "done" }, summary);
          if (state.kind === "refresh") pushFeed(summary);
          break;
        }
        case "error":
          t = pushLine(
            { ...t, status: "error", stage: "error" },
            `✗ ${data.message || "failed"}`,
          );
          if (state.kind === "refresh") pushFeed(`✗ ${data.message || "failed"}`);
          break;
      }
      tasks[idx] = t;
      return { tasks, feed };
    }),

  endRun: () => {
    const state = get();
    const allDone =
      state.kind === "refresh" &&
      state.tasks.length > 0 &&
      state.tasks.every((t) => t.status === "done" || t.status === "error");
    set({ active: false, allDone });
  },

  clearFinished: () =>
    set((state) => ({
      tasks: state.tasks.filter((t) => t.status !== "done" && t.status !== "error"),
    })),

  clearFeed: () => set({ feed: [], tasks: [], allDone: false, kind: null }),
}));

/** True while any task is still queued/running (grid should keep finished
 * cards visible so the sequential flow is readable until the run ends). */
export function hasOpenTasks(tasks: FetchTask[]): boolean {
  return tasks.some((t) => t.status === "queued" || t.status === "running");
}
