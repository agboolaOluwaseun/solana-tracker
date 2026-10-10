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
  birdeye_saved: number;  // unpriceables rescued via the Birdeye retry
  total_calls: number;
  /** Rolling narration lines — what just happened, real counters only. */
  log: string[];
  /** Last message appended (dedupe consecutive identical stage lines). */
  lastMessage: string;
  /** ms epoch of the last SSE event touching this task — drives the
   * stale-progress badge (MadApes 2026-10-06: card froze at 204/461 for
   * 90 min on a dead browser-side reader while the server priced to 461). */
  lastEventAt: number;
}

interface RunState {
  tasks: FetchTask[];
  active: boolean; // this kind's stream is open
  /** Rolling global narration (refresh kind: the bottom feed panel). */
  feed: FeedLine[];
  /** Set once a refresh stream ended cleanly — panel says all up to date. */
  allDone: boolean;
  /** Daily boot gate said "already updated today" — the run was a no-op
   * (server emitted `skipped`). Panel shows an honest one-liner instead
   * of a fake full queue. */
  skipped: boolean;
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
  return { tasks: [], feed: [], active: false, allDone: false, skipped: false };
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
    birdeye_saved: 0,
    total_calls: 0,
    log: [],
    lastMessage: "",
    lastEventAt: Date.now(),
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
    skipped: false,
  };
}

function applyEventFor(prev: RunState, kind: RunKind, data: SseEvent): RunState {
  // Daily boot gate no-op: the whole run was a single 'skipped' message.
  // Don't seed tasks, don't claim a completion the run didn't produce —
  // just narrate the honest one-liner.
  if (data.status === "skipped") {
    if (kind !== "refresh") return prev;
    return {
      ...prev,
      tasks: [],
      skipped: true,
      feed: [{ ch: "—", line: data.message || "Already updated today" }],
      active: true,
      allDone: false,
    };
  }
  // Resume notice from the gate: an interrupted run is continuing.
  if (data.status === "resumed") {
    if (kind !== "refresh") return prev;
    const line = data.message || "resuming interrupted refresh…";
    return { ...prev, feed: [...prev.feed, { ch: "—", line }].slice(-MAX_FEED) };
  }
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
  // Items may arrive already 'done' (gate resume: finished in the earlier,
  // interrupted pass) — seed them closed so the counter is honest.
  if (data.status === "queue" && data.channels) {
    const merged = data.channels.map((f) => {
      const base = prev.tasks.find((t) => t.channel_id === f.channel_id) ?? blankTask(f);
      return f.status === "done"
        ? { ...base, status: "done" as const, stage: "done",
            lastMessage: base.lastMessage || "done in earlier pass", }
        : { ...base, title: f.title };
    });
    const nDone = merged.filter((t) => t.status === "done").length;
    const nLeft = merged.length - nDone;
    const line = nDone
      ? `resuming: ${nDone} channel(s) already updated, ${nLeft} to go…`
      : `updating ${merged.length} channel${merged.length === 1 ? "" : "s"}…`;
    return {
      tasks: merged,
      feed:
        kind === "refresh"
          ? [...prev.feed, { ch: "—", line }].slice(-MAX_FEED)
          : prev.feed,
      active: true,
      allDone: false,
      skipped: false,
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

  let t = { ...tasks[idx], lastEventAt: Date.now() };
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
      if (typeof data.birdeye_saved === "number") t.birdeye_saved = data.birdeye_saved;
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
              t.birdeye_saved ? `, ${t.birdeye_saved} via birdeye` : ""
            }${t.waiting ? `, ${t.waiting} waiting` : ""}`);
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
      (prev.skipped || // daily boot gate no-op — panel shows its one-liner
        (prev.tasks.length > 0 &&
          prev.tasks.every((t) => t.status === "done" || t.status === "error"))),
  };
}

/* ── stream orchestration (module-scoped; not renderable state) ─────────── */

const controllers: Record<RunKind, AbortController | null> = {
  fetch: null,
  refresh: null,
};

/* Fetch-stream reconnect (user 2026-10-10 'frontend never lags behind'):
 * capped per page load so a down server can't make the tab loop. */
const MAX_FETCH_RECONNECTS = 3;
let fetchReconnectAttempts = 0;

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
    if (kind === "fetch") resetFetchReconnect();   // clean finish = fresh budget
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
      if (kind === "fetch" && fetchReconnectAttempts < MAX_FETCH_RECONNECTS) {
        // Reader died mid-fetch (proxy hiccup, tab sleep, wifi drop): the
        // server kept the work AND fetch_queue kept the channel. Re-attach
        // shortly; the queue is authoritative about what is still pending.
        fetchReconnectAttempts += 1;
        const delay = 3000 * fetchReconnectAttempts;
        setTimeout(() => {
          void (async () => {
            try {
              const pend = await fetch(`${API_BASE}/api/pending-fetches`).then((r) =>
                r.ok ? (r.json() as Promise<{ channel_id: number; title: string }[]>) : [],
              );
              if (pend.length > 0 && !controllers.fetch) {
                await runFetchStream(
                  pend.map((p) => p.channel_id),
                  pend,
                );
              }
            } catch {
              /* next attempt or the stale badge cover it */
            }
          })();
        }, delay);
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
 * the server makes the refresh's Telegram scans yield around the fetch.
 * `force` (the manual button) bypasses the server's once-per-day boot gate
 * and always runs; boot callers use the default false. */
export async function runRefreshStream(force = false): Promise<boolean> {
  if (useFetchStore.getState().refresh.active) return false;
  await runStream("refresh", "/api/refresh-stream", force ? { force: true } : {}, []);
  return true;
}

/** Boot auto-refresh: at most ONE attempt per app load. Module state
 * survives client-side navigation (deep-dive -> back does NOT remount JS),
 * so page-to-page movement no longer re-fires the boot POST. Only a real
 * reload (new tab, refresh, relaunch) retries — which is what 'on launch'
 * should mean. (Previously the homepage effect fired on every remount:
 * round-tripping from a channel deep-dive re-ran the stream; with the
 * gate never closing on a permanently-broken channel that looked like
 * 'rescoring finished, then started all over'.) */
let bootAttemptedThisLoad = false;
export async function runBootRefreshOnce(): Promise<boolean> {
  if (bootAttemptedThisLoad) return false;
  bootAttemptedThisLoad = true;
  // Interrupted-fetch resume goes FIRST (user rule 2026-10-10): pending
  // queue items are re-streamed so their cards spin again exactly like a
  // fresh fetch (the scan re-walks cheaply — stored rows are skipped —
  // and pricing picks up exactly the still-pending calls = 'from where it
  // stopped'); only after that finishes does the usual daily refresh run.
  try {
    const res = await fetch(`${API_BASE}/api/pending-fetches`);
    if (res.ok) {
      const pend = (await res.json()) as
        Array<{ channel_id: number; title: string; days?: number | null }>;
      if (Array.isArray(pend) && pend.length > 0) {
        await runFetchStream(
          pend.map((p) => p.channel_id),
          pend.map((p) => ({ channel_id: p.channel_id, title: p.title })),
          pend[0].days ?? undefined,
        );
      }
    }
  } catch {
    /* server down — the refresh attempt below surfaces the real error */
  }
  return runRefreshStream(false);
}

/** Dropdown fetch — starts IMMEDIATELY, even while a refresh is running. */
export async function runFetchStream(
  channelIds: number[],
  items: { channel_id: number; title: string }[],
  days?: number,
): Promise<void> {
  await runStream("fetch", "/api/fetch-stream",
    { channel_ids: channelIds, ...(days ? { days } : {}) }, items);
}

/** User dismissed the panel mid-refresh: stop ONLY the refresh stream.
 * We deliberately do NOT null the controller here — the stream's own
 * finally block (which fires when the abort propagates) still recognizes
 * itself as the owner and runs endRun, so `active` can never get stuck. */
/** Called by runStream when a fetch run completed cleanly — resets the
 * reconnect budget so a later genuine hiccup in the same tab still gets
 * its retries. */
function resetFetchReconnect() {
  fetchReconnectAttempts = 0;
}

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
