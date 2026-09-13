# GeckoTerminal Rate Limiter Analysis

## Current Algorithm Breakdown

### Rate Limiter (rate_limiter.py)

**Configuration:**
- Advertised RPM: 20 (free tier)
- Safety margin: 0.65 (65%)
- **Sustained target: 13 RPM**
- Min interval: 60/13 = 4.6 seconds between requests
- Jitter range: 0.3-1.5s (in `acquire()`)

**Circuit Breaker:**
- First 429: 20s cooldown, drops target to 0.6× (7.8 RPM)
- Second 429: 40s cooldown, drops to 0.6× again (4.7 RPM)
- Third 429: 80s cooldown, drops to 0.6× again (2.8 RPM)
- Max cooldown: 300s (5 minutes)
- Reset: requires 8 consecutive clean requests

### Request Flow (geckoterminal.py)

Each `_request()` call:
1. `limiter.acquire()` → blocks 4.6s + jitter 0.3-1.5s
2. `time.sleep(random.uniform(0.4, 1.8))` → **ADDITIONAL jitter** 0.4-1.8s
3. HTTP request (100-300ms latency)
4. On 429: `notify_throttled()` + sleep 5-30s
5. tenacity retry: exponential backoff (4s, 8s, 16s, 32s, 64s, 120s)

**Problem: Double Jitter**
- Limiter adds 0.3-1.5s jitter in `acquire()`
- `_request()` adds another 0.4-1.8s jitter
- **Total jitter: 0.7-3.3s per request** (avg 2.0s)

### Backtest Flow (backtest.py)

For each call:
1. `get_cached_pool()` → DB lookup (fast, ~1ms)
2. If not cached: `resolve_pool_smart()` → **1-3 API calls**
   - `/search/pools` (1 call)
   - If not found: `/tokens/{addr}/pools` (1 call)
   - If still not found: `/pools/{addr}` (1 call)
3. `fetch_ohlcv()` → **1-2 API calls** (cache-first with gap-filling)
   - Fetches missing spans from cache
   - Merges with cached data

**Request Count for 142 Calls:**
- Best case (all pools cached): 142 × 1 = **142 requests** (OHLCV only)
- Typical case (some pool resolution): 142 × 2.5 = **355 requests**
- Worst case (all need resolution + retries): 142 × 4 = **568 requests**

## Measured vs Theoretical Performance

**Theoretical (284 requests at 13 RPM):**
- Base rate: 284 / 13 RPM = **21.8 minutes**
- Plus jitter: 284 × 1.1s avg = 312s = **5.2 minutes**
- Plus 429 cascades: 10 throttles × 20s avg = 200s = **3.3 minutes**
- Plus tenacity retries: 5% failure × 14 reqs × 20s = 140s = **2.3 minutes**
- **Total theoretical: ~32.6 minutes**

**Measured: 3+ hours (180+ minutes)**

**Gap: 5.5x slower than theoretical**

## Root Causes

### 1. Double Jitter (Major)
The limiter already adds jitter in `acquire()`, but `_request()` adds **another** 0.4-1.8s sleep. This adds ~0.9s extra per request on average.

**Impact:** 284 requests × 0.9s = 256s = **4.3 minutes wasted**

### 2. Circuit Breaker Death Spiral (Critical)
A single 429 triggers:
- 20s cooldown (blocks ALL requests)
- Target drops to 7.8 RPM (slower)
- Next 429: 40s cooldown, target = 4.7 RPM
- Third 429: 80s cooldown, target = 2.8 RPM

**One bad minute ruins the next hour.**

**Impact:** If you hit 3 consecutive 429s (common at 17+ RPM):
- 20s + 40s + 80s = **140s = 2.3 minutes blocked**
- Target drops to 2.8 RPM → 284 requests now take **101 minutes**
- **Total: 103 minutes instead of 22 minutes**

### 3. tenacity + Circuit Breaker Conflict (Major)
Both add backoff independently:
- 429 triggers circuit breaker: 20s cooldown
- tenacity also waits: 4s, then 8s, then 16s
- **Total: 20s + 4s = 24s for first retry** (not 20s)

**Impact:** ~50% longer cooldowns than intended

### 4. Overly Conservative Safety Margin (Moderate)
Safety margin 0.65 targets 13 RPM when the API's real ceiling is ~23 RPM (per rate_limiter.py comments). We're leaving 40% of capacity unused.

**Impact:** 284 requests at 13 RPM = 22 min, at 17 RPM = 17 min. **5 minutes wasted.**

### 5. No Parallelism (Major)
Single-threaded execution. Network latency (100-300ms) is wasted time that could overlap with rate-limited gaps.

**Impact:** Could cut time by 3-4x with parallel workers

### 6. Sequential Pool Resolution (Moderate)
Each call resolves its pool before fetching OHLCV. Could batch all pool resolutions upfront.

**Impact:** 142 pool resolutions × 2.5s avg = 355s = **5.9 minutes** (could be parallelized)

## Proposed Solutions

### Solution A: Conservative Improvements (Low Risk)

**Changes:**
1. Increase safety margin: 0.65 → **0.85** (17 RPM)
2. Remove double jitter: delete `time.sleep(0.4-1.8)` in `_request()`
3. Reduce circuit breaker aggression:
   - Min cooldown: 20s → **8s**
   - Growth: 2.0× → **1.5×**
   - Rate drop: 0.6× → **0.75×**
   - Max cooldown: 300s → **120s**
4. Adaptive boost: after 20 clean requests, push to 0.90× (18 RPM)

**Expected Result:** 20-30 minutes (down from 3+ hours)

**Risk:** Low. Still conservative, just less paranoid.

---

### Solution B: Parallel Fetch with Shared Limiter (Medium Risk)

**Architecture:**
```python
from concurrent.futures import ThreadPoolExecutor

def backtest_calls_parallel(calls, max_workers=4):
    limiter = RateLimiter(20, safety_margin=0.85)
    client = GeckoTerminalClient()
    client.limiter = limiter  # Share limiter across workers
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(backtest_call, client, call.token_address, call.call_ts) 
                   for call in calls]
        results = [f.result() for f in futures]
    return results
```

**How It Works:**
- 4 workers process calls in parallel
- All share the same `RateLimiter` instance
- Worker 1 waits 4.6s, makes request
- Worker 2 waits 4.6s, makes request (but starts while Worker 1 is waiting)
- **Overlaps network latency (100ms) with rate-limited gaps (4.6s)**

**Expected Result:** 10-15 minutes (3-4x speedup from Solution A)

**Risk:** Medium. Need to ensure thread-safe DB writes and cache updates.

---

### Solution C: Two-Phase Batch Processing (Medium Risk)

**Phase 1: Prefetch All Pools (Parallel)**
```python
def prefetch_pools(calls, max_workers=5):
    """Resolve all pools before fetching OHLCV."""
    unique_tokens = {call.token_address for call in calls}
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(client.resolve_pool_smart, addr): addr 
                   for addr in unique_tokens}
        for future in as_completed(futures):
            addr = futures[future]
            pool_info = future.result()
            if pool_info:
                upsert_token_meta(pool_info)
```

**Phase 2: Fetch OHLCV (Sequential, Rate-Limited)**
```python
def backtest_phase2(calls):
    """All pools are cached. Just fetch OHLCV."""
    for call in calls:
        backtest_call(client, call.token_address, call.call_ts)
```

**How It Works:**
- Phase 1: Resolve 142 pools in parallel (5 workers, ~30s)
- Phase 2: Fetch OHLCV sequentially (rate-limited, ~15 min)
- **Total: ~16 minutes**

**Expected Result:** 15-20 minutes

**Risk:** Medium. Requires checkpointing between phases.

---

### Solution D: Token Bucket + Adaptive Rate (Novel, High Reward)

**Replace RateLimiter with Token Bucket:**
```python
class TokenBucketLimiter:
    def __init__(self, rate_rpm, burst_size):
        self.rate = rate_rpm / 60.0  # tokens per second
        self.capacity = burst_size
        self.tokens = burst_size
        self.last_update = time.monotonic()
    
    def acquire(self):
        now = time.monotonic()
        elapsed = now - self.last_update
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_update = now
        
        if self.tokens < 1.0:
            wait = (1.0 - self.tokens) / self.rate
            time.sleep(wait)
            self.tokens = 0.0
        else:
            self.tokens -= 1.0
```

**Adaptive Rate:**
- Start at 18 RPM (90% of 20)
- On 429: drop to 15 RPM for 30s, then back to 18
- No jitter needed (token bucket is naturally smooth)
- No circuit breaker (just exponential backoff on 429)

**How It Works:**
- Token bucket allows short bursts (up to `burst_size`)
- Smooth rate limiting without jitter
- Adaptive rate responds to 429s without death spiral

**Expected Result:** 12-18 minutes

**Risk:** High. Novel approach, needs testing.

---

### Solution E: Dual-API Pipeline (Novel, High Reward)

**Use Birdeye for Pool Resolution:**
```python
def resolve_pool_birdeye(token_address):
    """Birdeye: direct token endpoint, no rate limit issues."""
    resp = birdeye_session.get(
        f"https://public-api.birdeye.so/defi/token/overview",
        params={"address": token_address},
        timeout=10
    )
    data = resp.json()["data"]
    return {
        "pool_address": data.get("poolAddress"),
        "token_address": token_address,
        "symbol": data.get("symbol"),
        "liquidity_usd": data.get("liquidity"),
    }
```

**Pipeline:**
1. Resolve all pools via Birdeye (fast, 1 req/s, ~142s)
2. Fetch OHLCV via GeckoTerminal (rate-limited, ~15 min)

**How It Works:**
- Birdeye has no aggressive rate limit (50k CU/month, 35 CU/request)
- Birdeye's `/defi/token/overview` returns pool address directly
- GeckoTerminal only handles OHLCV (no pool resolution overhead)

**Expected Result:** 15-20 minutes

**Risk:** Medium. Birdeye's pool address format may differ.

---

### Solution F: Predictive Caching + Request Deduplication (Novel)

**Predictive Caching:**
```python
def prefetch_all(calls):
    """Before backtest starts, prefetch all unique tokens."""
    unique_tokens = {call.token_address for call in calls}
    
    # Prefetch pools in parallel
    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(client.resolve_pool_smart, unique_tokens))
    
    # Prefetch OHLCV for all tokens (sequential, rate-limited)
    for token in unique_tokens:
        client.fetch_ohlcv(...)
```

**Request Deduplication:**
```python
class DeduplicatingLimiter:
    def __init__(self, base_limiter):
        self.base = base_limiter
        self.in_flight = {}  # URL -> Future
    
    def acquire(self, url):
        if url in self.in_flight:
            return self.in_flight[url].result()  # Wait for existing request
        
        future = Future()
        self.in_flight[url] = future
        self.base.acquire()
        result = make_request(url)
        future.set_result(result)
        del self.in_flight[url]
        return result
```

**How It Works:**
- Before backtest: prefetch all 142 unique tokens (parallel pool resolution)
- During backtest: if two calls need the same OHLCV, share the result
- Eliminates redundant fetches for cross-channel duplicates

**Expected Result:** 10-15 minutes

**Risk:** High. Complex implementation.

---

## Recommendation

**Start with Solution A** (conservative improvements):
- Easy to implement
- Low risk
- Cuts time from 3+ hours to 20-30 minutes

**Then add Solution C** (two-phase batch):
- Prefetch pools in parallel
- Cuts time to 15-20 minutes

**Long-term: Solution D** (token bucket + adaptive):
- Most elegant
- Cuts time to 12-18 minutes
- Requires thorough testing

**Avoid:**
- Double jitter (remove it)
- Aggressive circuit breaker (reduce cooldowns)
- Sequential pool resolution (parallelize it)

## Implementation Plan

### Phase 1: Quick Wins (30 min work)
1. Create `rate_limiter_v2.py` with:
   - Safety margin 0.85
   - Reduced jitter (0.1-0.5s)
   - Shorter cooldowns (8s min, 1.5× growth)
   - Adaptive boost after 20 clean requests

2. Create `geckoterminal_v2.py` with:
   - Remove `time.sleep(0.4-1.8)` in `_request()`
   - Use `RateLimiterV2`

3. Test on 5 calls from 666 🔥 Calls
   - Measure time
   - Verify no 429s

### Phase 2: Parallel Pool Resolution (1 hour work)
1. Add `prefetch_pools()` function
2. Modify pipeline to prefetch before backtest
3. Test on 10 calls

### Phase 3: Production Rollout (30 min work)
1. Replace old limiter with V2
2. Add prefetch to main pipeline
3. Monitor for 1 week

**Expected Outcome:** 3+ hours → 15-20 minutes (10x speedup)
