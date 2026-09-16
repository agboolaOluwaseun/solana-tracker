/**
 * Shared fetch state + stream orchestration — one store read by BOTH the
 * "Fetch channels" dropdown (ChannelSelector) and the channel grid cards /
 * bottom update feed (page.tsx), so the same in-flight run can never render
 * twice with different progress.
 *
 * The old bug this fixes: ChannelSelector kept a LOCAL tasks state keyed by
 * the Telegram channel id (what the dropdown knows), while page.tsx created
 * a second state keyed by the server event's DB primary key — two cards per
 * channel whose progress updated inconsistently. Now there is ONE keyed list
 * per run kind; the server's `start` event (DB pk) re-keys a queued card in
 * place.
 *
 * Run kinds run CONCURRENTLY (user decision 2026-09-16: "fetch starts
 * immediately, not waiting at all for anything"):
 *   fetch   — dropdown picks (5-month backfill); progress → grid cards.
 *   refresh — boot auto-update + "Refresh All" (delta since last call);
 *             progress → bottom "Updating channels" feed.
 * A user fetch never aborts the refresh and vice versa. Telegram contention
 * is solved SERVER-side (ingestion/telegram_guard): the refresh's message
 * scans yield within ~one batch while a user fetch holds the session, then
 * the server retries the yielded channel after the fetch finishes. Parsing,
 * pricing and DB work always run fully concurrent (WAL + shared,
 * thread-safe API rate limiters).
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

interface RunState {
  tasks: FetchTask[];
  active: boolean; // this kind's stream is open
  /** Rolling global narration (refresh kind: the bottom feed panel). */
  feed: FeedLine[];
  /** Set once a refresh stream ended cleanly — panel says all up to date. */
  allDone: boolean;
}

interface FetchState {
  fetch: RunState;
  refresh: RunState;
  beginRun: (kind: RunKind, items: { channel_id: number; title: string }[]) => void;
  /** Apply one SSE event payload (queue | start | progress | done | error |
   * rescore_start | rescore_done) to the given run kind. */
  applyEvent: (kind: RunKind, data: SseEvent) => void;
  endRun: (kind: RunKind) => void;
  /** Remove done/error FETCH cards once the grid reloads (refresh tasks feed
   * the panel's n/total counter and must survive reloads). */
  clearFinished: () => void;
  /** Hide the bottom panel / reset refresh state. */
  clearFeed: () => void;
}

const MAX_LOG = 12;
const MAX_FEED = 80;

function blankRun(): RunState {
  return { tasks: [], feed: [], active: false, allDone: false };
}

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

/* ── per-kind reducers ─────────────────────────────────────────────────── */

function beginRunFor(prev: RunState, items: { channel_id: number; title: string }[], kind: RunKind): RunState {
  return {
    tasks: items.length ? items.map(blankTask) : prev.tasks,
    feed:
      kind === "refresh" && items.length
        ? [
            {
              ch: "—",
              line: `updating ${items.length} channel${items.length === 1 ? "" : "s"}…`,
            },
          ]
        : kind === "refresh"
          ? prev.feed
          : [],
    active: true,
    allDone: false,
  };
}

function applyEventFor(prev: RunState, kind: RunKind, data: SseEvent): RunState {
  // Stream-level rescore narration (refresh runs only).
  if (data.status === "rescore_start" || data.status === "rescore_done") {
    if (kind !== "refresh") return prev;
    const line =
      data.status === "rescore_start"
        ? "refreshing live verdicts (calls younger than 7 days)…"
        : `live verdicts refreshed — ${data.updated ?? 0} calls updated`;
    return { ...prev, feed: [...prev.feed, { ch: "—", line }].slice(-MAX_FEED) };
  }
  // Rescore per-call progress carries no channel_id (it's DB-wide) —
  // narrate it to the feed so the footer never looks frozen (GT's ~5/min
  // pace makes this pass legitimately slow).
  if (data.status === "progress" && data.stage === "rescore") {
    if (kind !== "refresh") return prev;
    const line = data.message || "rescoring live calls…";
    const last = prev.feed[prev.feed.length - 1];
    if (last && last.ch === "—" && last.line === line) return prev;
    return { ...prev, feed: [...prev.feed, { ch: "—", line }].slice(-MAX_FEED) };
  }
  // 'queue' carries the whole channel list (no channel_id) — seed tasks.
  if (data.status === "queue" && data.channels) {
    const merged = data.channels.map(
      (f) => prev.tasks.find((t) => t.channel_id === f.channel_id) ?? blankTask(f),
    );
    const line = `updating ${merged.length} channel${merged.length === 1 ? "" : "s"}…`;
    return {
      tasks: merged,
      feed:
        kind === "refresh"
          ? [...prev.feed, { ch: "—", line }].slice(-MAX_FEED)
          : prev.feed,
      active: true,
      allDone: false,
    };
  }
  if (!data.channel_id) return prev;

  // Re-key: the client initially queues by whatever id it had (could be a
  // Telegram id for freshly-added channels); the server's events carry the
  // authoritative DB primary key. Match by position of the first
  // queued/running task when ids don't line up, then stamp the real id.
  let idx = prev.tasks.findIndex((t) => t.channel_id === data.channel_id);
  const tasks = [...prev.tasks];
  if (idx === -1 && (data.status === "start" || data.status === "progress")) {
    const firstOpen = tasks.findIndex(
      (t) => t.status === "queued" || t.status === "running",
    );
    if (firstOpen !== -1) {
      tasks[firstOpen] = { ...tasks[firstOpen], channel_id: data.channel_id! };
      idx = firstOpen;
    }
  }
  if (idx === -1) return prev;

  let t = { ...tasks[idx] };
  if (data.title) t.title = data.title;

  // Bottom-panel narration: refresh runs mirror every channel-tagged
  // progress line (fetch runs stay card-only, as designed).
  let feed = prev.feed;
  const pushFeed = (line: string) => {
    const last = feed[feed.length - 1];
    if (last && last.ch === t.title && last.line === line) return;
    feed = [...feed, { ch: t.title, line }].slice(-MAX_FEED);
  };

  switch (data.status) {
    case "start":
      t = pushLine({ ...t, status: "running", stage: "fetch" }, "starting…");
      if (kind === "refresh") pushFeed("starting…");
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
        if (kind === "refresh") pushFeed(data.message);
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
      if (kind === "refresh") pushFeed(summary);
      break;
    }
    case "error":
      t = pushLine(
        { ...t, status: "error", stage: "error" },
        `✗ ${data.message || "failed"}`,
      );
      if (kind === "refresh") pushFeed(`✗ ${data.message || "failed"}`);
      break;
  }
  tasks[idx] = t;
  return { ...prev, tasks, feed };
}

function endRunFor(prev: RunState, kind: RunKind): RunState {
  return {
    ...prev,
    active: false,
    allDone:
      kind === "refresh" &&
      prev.tasks.length > 0 &&
      prev.tasks.every((t) => t.status === "done" || t.status === "error"),
  };
}

/* ── stream orchestration (module-scoped; not renderable state) ─────────── */

const controllers: Record<RunKind, AbortController | null> = {
  fetch: null,
  refresh: null,
};

/** Open a stream for one kind. Only the SAME kind's previous stream is
 * aborted (a second dropdown fetch replaces the first); the other kind keeps
 * running untouched — the server arbitrates Telegram access. */
async function runStream(
  kind: RunKind,
  path: string,
  body: unknown,
  items: { channel_id: number; title: string }[],
): Promise<void> {
  controllers[kind]?.abort();
  const myController = new AbortController();
  controllers[kind] = myController;
  const s = useFetchStore.getState();
  s.beginRun(kind, items);
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
      // Refresh's queue event carries the item list the client didn't seed.
      useFetchStore.getState().applyEvent(kind, data);
    });
  } catch (err) {
    if (controllers[kind] === myController && err instanceof Error && err.name !== "AbortError") {
      if (kind === "refresh") {
        useFetchStore.setState((st) => ({
          refresh: {
            ...st.refresh,
            feed: [
              ...st.refresh.feed,
              { ch: "—", line: `✗ update failed: ${err.message}` },
            ].slice(-MAX_FEED),
          },
        }));
      }
      throw err;
    }
    if (err instanceof Error && err.name !== "AbortError") throw err;
  } finally {
    if (controllers[kind] === myController) {
      controllers[kind] = null;
      useFetchStore.getState().endRun(kind);
    }
  }
}

/** Boot auto-update + "Refresh All". Returns false if a refresh is already
 * running (the caller must not claim a fresh completion it didn't produce).
 * A concurrent user fetch does NOT block it — both streams run side by side;
 * the server makes the refresh's Telegram scans yield around the fetch. */
export async function runRefreshStream(): Promise<boolean> {
  if (useFetchStore.getState().refresh.active) return false;
  await runStream("refresh", "/api/refresh-stream", {}, []);
  return true;
}

/** Dropdown fetch — starts IMMEDIATELY, even while a refresh is running. */
export async function runFetchStream(
  channelIds: number[],
  items: { channel_id: number; title: string }[],
): Promise<void> {
  await runStream("fetch", "/api/fetch-stream", { channel_ids: channelIds }, items);
}

/** User dismissed the panel mid-refresh: stop ONLY the refresh stream.
 * We deliberately do NOT null the controller here — the stream's own
 * finally block (which fires when the abort propagates) still recognizes
 * itself as the owner and runs endRun, so `active` can never get stuck. */
export function stopRefresh() {
  controllers.refresh?.abort();
}

export const useFetchStore = create<FetchState>((set) => ({
  fetch: blankRun(),
  refresh: blankRun(),

  beginRun: (kind, items) =>
    set((state) => ({
      [kind]: beginRunFor(state[kind], items, kind),
    }) as Pick<FetchState, RunKind>),

  applyEvent: (kind, data) =>
    set((state) => ({
      [kind]: applyEventFor(state[kind], kind, data),
    }) as Pick<FetchState, RunKind>),

  endRun: (kind) =>
    set((state) => ({
      [kind]: endRunFor(state[kind], kind),
    }) as Pick<FetchState, RunKind>),

  clearFinished: () =>
    set((state) => ({
      fetch: {
        ...state.fetch,
        tasks: state.fetch.tasks.filter(
          (t) => t.status !== "done" && t.status !== "error",
        ),
      },
    })),

  clearFeed: () => set({ refresh: blankRun() }),
}));
