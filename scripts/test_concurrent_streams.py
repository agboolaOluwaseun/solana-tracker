# Live proof: refresh + user fetch run CONCURRENTLY; fetch never waits.
import json, threading, time, urllib.request

BASE = "http://localhost:8777"

def stream(path, body, sink, stop_at=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            for raw in r:
                line = raw.decode().strip()
                if not line.startswith("data: "): continue
                d = json.loads(line[6:])
                sink.append((round(time.time() - t0, 1), d))
                if stop_at and stop_at(d): break
    except Exception as e:
        sink.append((round(time.time() - t0, 1), {"status": "CLIENT_CLOSED" if "timed out" not in str(e).lower() else "TIMEOUT", "err": str(e)[:60]}))

ref, fch = [], []

def wait_for_first_fetch_progress():
    deadline = time.time() + 300
    while time.time() < deadline:
        if any(d.get("status") == "progress" for _, d in fch):
            return True
        time.sleep(0.5)
    return False

# 1) boot refresh starts
th_r = threading.Thread(target=stream, args=("/api/refresh-stream", {}, ref,
        lambda d: d.get("status") == "rescore_done"))
th_r.start()
time.sleep(8)   # let the refresh get a few channels in

# 2) user fetch fires WHILE refresh runs — must start progressing immediately
fch_stop = lambda d: d.get("status") == "complete"
th_f = threading.Thread(target=stream, args=("/api/fetch-stream", {"channel_ids": [31]}, fch, fch_stop))
t_fire = time.time()
th_f.start()

# measure: when did the fetch emit its FIRST progress event?
ok = wait_for_first_fetch_progress()
first = next((t for t, d in fch if d.get("status") == "progress"), None)
print(f"fetch first-progress after {first}s (started while refresh running): {'YES' if ok else 'NO'}")

th_f.join(timeout=900)
# 3) refresh should then finish its retry lane and complete
th_r.join(timeout=600)

def summarize(sink, name):
    kinds = {}
    yield_lines, retry_lines, paused = [], [], []
    for t, d in sink:
        s = d.get("status")
        kinds[s] = kinds.get(s, 0) + 1
        m = (d.get("message") or "")
        if "yielded" in m or d.get("stage") == "yielded": paused.append((t, d.get("channel_id"), m[:70]))
        if "waiting for your fetch" in m: retry_lines.append((t, m[:70]))
        if s == "done" and "skipped" in m: yield_lines.append((t, m[:90]))
    print(f"\n[{name}] {len(sink)} events:", dict(kinds))
    if paused: print("  paused (yielded) lines:", paused[:4])
    if retry_lines: print("  retry-lane waiting:", retry_lines[:2])
    if yield_lines: print("  skipped-closes:", yield_lines[:2])
    # did fetch start while refresh was still streaming?
summarize(ref, "refresh")
summarize(fch, "fetch")

fetch_done_t = next((t for t, d in fch if d.get("status") == "complete"), None)
refresh_last_t = ref[-1][0] if ref else None
print(f"\nfetch complete at ~{fetch_done_t}s; refresh last event at ~{refresh_last_t}s "
      f"-> overlap={'YES (concurrent)' if fetch_done_t and refresh_last_t and fetch_done_t < refresh_last_t else 'inconclusive/sequential'}")
