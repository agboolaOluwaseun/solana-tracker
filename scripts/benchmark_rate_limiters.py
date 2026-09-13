"""
Benchmark script: compare old vs new rate limiter.

Simulates 284 requests (142 calls × 2 requests each) through both limiters.
Measures total time and throttle events.

Run: python scripts/benchmark_rate_limiters.py
"""
import sys
import time
from datetime import datetime

sys.path.insert(0, "/Users/agboolaoluwaseun/ZCodeProject/solana_tracker_v2")

from pricing.rate_limiter import RateLimiter
from pricing.rate_limiter_v2 import RateLimiterV2


def simulate_requests(limiter, num_requests: int, throttle_rate: float = 0.0):
    """
    Simulate N requests through a limiter.
    
    throttle_rate: probability of a 429 on each request (0.0-1.0)
    Returns: (total_time, throttle_count)
    """
    import random
    
    start = time.monotonic()
    throttle_count = 0
    
    for i in range(num_requests):
        limiter.acquire()
        
        # Simulate API response
        time.sleep(0.05)  # 50ms network latency
        
        # Simulate throttle
        if random.random() < throttle_rate:
            throttle_count += 1
            limiter.notify_throttled(retry_after_hint=0.0)
        else:
            limiter.notify_success()
    
    elapsed = time.monotonic() - start
    return elapsed, throttle_count


def benchmark(num_requests=284, throttle_rate=0.05):
    """Run benchmark with both limiters."""
    print(f"Benchmark: {num_requests} requests, {throttle_rate*100:.0f}% throttle rate\n")
    
    # Old limiter
    print("Testing OLD limiter (safety=0.65, jitter=0.4-1.8s)...")
    old_limiter = RateLimiter(requests_per_minute=20, safety_margin=0.65)
    old_time, old_throttles = simulate_requests(old_limiter, num_requests, throttle_rate)
    old_rpm = num_requests / (old_time / 60)
    
    print(f"  Time: {old_time:.1f}s ({old_time/60:.1f} min)")
    print(f"  Effective RPM: {old_rpm:.1f}")
    print(f"  Throttles: {old_throttles}")
    print(f"  Final target RPM: {old_limiter.target_rpm}")
    print()
    
    # New limiter
    print("Testing NEW limiter (safety=0.85, jitter=0.1-0.5s)...")
    new_limiter = RateLimiterV2(requests_per_minute=20, safety_margin=0.85)
    new_time, new_throttles = simulate_requests(new_limiter, num_requests, throttle_rate)
    new_rpm = num_requests / (new_time / 60)
    
    print(f"  Time: {new_time:.1f}s ({new_time/60:.1f} min)")
    print(f"  Effective RPM: {new_rpm:.1f}")
    print(f"  Throttles: {new_throttles}")
    print(f"  Final target RPM: {new_limiter.target_rpm}")
    print(f"  Stats: {new_limiter.get_stats()}")
    print()
    
    # Comparison
    speedup = old_time / new_time
    print(f"Speedup: {speedup:.2f}x faster")
    print(f"Time saved: {(old_time - new_time)/60:.1f} minutes")
    
    return {
        "old": {"time": old_time, "rpm": old_rpm, "throttles": old_throttles},
        "new": {"time": new_time, "rpm": new_rpm, "throttles": new_throttles},
        "speedup": speedup,
    }


if __name__ == "__main__":
    print("=" * 70)
    print("Rate Limiter Benchmark")
    print("=" * 70)
    print()
    
    # Scenario 1: Low throttle rate (5%)
    print("SCENARIO 1: Low throttle rate (5%)")
    print("-" * 70)
    benchmark(num_requests=284, throttle_rate=0.05)
    print()
    
    # Scenario 2: Medium throttle rate (10%)
    print("SCENARIO 2: Medium throttle rate (10%)")
    print("-" * 70)
    benchmark(num_requests=284, throttle_rate=0.10)
    print()
    
    # Scenario 3: High throttle rate (20%)
    print("SCENARIO 3: High throttle rate (20%)")
    print("-" * 70)
    benchmark(num_requests=284, throttle_rate=0.20)
