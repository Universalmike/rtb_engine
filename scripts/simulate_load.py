"""
RTB Load Simulator
==================
Simulates real-world bid request traffic against the auction engine.

Usage:
    python scripts/simulate_load.py --rps 10 --duration 60
    python scripts/simulate_load.py --rps 20 --duration 30 --url http://localhost:8000

Windows compatible. Uses a semaphore to cap concurrency so the local
Docker backend isn't overwhelmed. Good numbers for local dev: 5-15 rps.
"""

import asyncio
import httpx
import random
import time
import argparse
import sys
import platform
from datetime import datetime
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────────

COUNTRIES = ["US", "GB", "NG", "DE", "CA", "AU", "FR", "BR", "IN", "JP"]
DEVICES   = ["desktop", "mobile", "tablet"]
PAGES     = [
    "https://techcrunch.com/article/ai-funding",
    "https://sportsnews.com/premier-league",
    "https://finance.com/markets/crypto",
    "https://entertainment.com/movies/2024",
    "https://news.com/world/breaking",
]

# ── Stats tracker ─────────────────────────────────────────────────────────────

class Stats:
    def __init__(self):
        self.total      = 0
        self.success    = 0
        self.errors     = 0
        self.fills      = 0
        self.latencies  = []
        self.start_time = time.monotonic()
        self._lock      = asyncio.Lock()

    async def record(self, latency_ms: float, filled: bool, error: bool = False):
        async with self._lock:
            self.total += 1
            if error:
                self.errors += 1
            else:
                self.success += 1
                self.latencies.append(latency_ms)
                if filled:
                    self.fills += 1

    def percentile(self, pct: float) -> float:
        if not self.latencies:
            return 0.0
        s = sorted(self.latencies)
        idx = int(len(s) * pct / 100)
        return s[min(idx, len(s) - 1)]

    def elapsed(self) -> float:
        return time.monotonic() - self.start_time

    def rps_actual(self) -> float:
        e = self.elapsed()
        return self.total / e if e > 0 else 0

    def fill_rate(self) -> float:
        return (self.fills / self.success * 100) if self.success > 0 else 0

    def summary(self) -> str:
        return (
            f"  Requests:   {self.total:,} total | {self.success:,} ok | {self.errors:,} errors\n"
            f"  Throughput: {self.rps_actual():.1f} req/s actual\n"
            f"  Fill rate:  {self.fill_rate():.1f}%\n"
            f"  Latency:    P50={self.percentile(50):.1f}ms  "
            f"P95={self.percentile(95):.1f}ms  P99={self.percentile(99):.1f}ms\n"
            f"  Elapsed:    {self.elapsed():.1f}s"
        )


# ── Core request ──────────────────────────────────────────────────────────────

async def fire_bid(client: httpx.AsyncClient, slot_ids: list, stats: Stats, base_url: str):
    payload = {
        "ad_slot_id":  random.choice(slot_ids),
        "country":     random.choice(COUNTRIES),
        "device_type": random.choice(DEVICES),
        "page_url":    random.choice(PAGES),
        "user_agent":  "Mozilla/5.0 (RTB-Simulator/1.0)",
    }
    t0 = time.monotonic()
    try:
        resp = await client.post(
            f"{base_url}/api/v1/auction/bid", json=payload, timeout=10.0
        )
        latency = (time.monotonic() - t0) * 1000
        if resp.status_code == 200:
            data = resp.json()
            await stats.record(latency, filled=data.get("had_fill", False))
        else:
            await stats.record(latency, filled=False, error=True)
    except Exception:
        latency = (time.monotonic() - t0) * 1000
        await stats.record(latency, filled=False, error=True)


# ── Rate-limited dispatcher ────────────────────────────────────────────────────

async def dispatcher(base_url: str, rps: int, duration: int, slot_ids: list):
    stats    = Stats()
    interval = 1.0 / rps
    end_time = time.monotonic() + duration

    # Semaphore caps concurrent in-flight requests.
    # Local Docker can't handle 50 concurrent DB connections —
    # cap at 10 to keep latency low and errors near zero.
    max_concurrent = min(rps, 10)
    semaphore = asyncio.Semaphore(max_concurrent)

    print(f"\n🚀 RTB Load Simulator")
    print(f"   Target:      {rps} req/s for {duration}s → ~{rps * duration:,} requests")
    print(f"   Concurrency: max {max_concurrent} in-flight")
    print(f"   Backend:     {base_url}")
    print(f"   Slots:       {len(slot_ids)} ad slots in rotation")
    print(f"   Platform:    {platform.system()}")
    print(f"   Started:     {datetime.now().strftime('%H:%M:%S')}\n")

    last_print = time.monotonic()

    async def guarded_bid(client):
        async with semaphore:
            await fire_bid(client, slot_ids, stats, base_url)

    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=max_concurrent + 5),
        timeout=10.0,
    ) as client:
        tasks = set()

        while time.monotonic() < end_time:
            task = asyncio.create_task(guarded_bid(client))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

            now = time.monotonic()
            if now - last_print >= 5.0:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Live stats:")
                print(stats.summary())
                print()
                last_print = now

            await asyncio.sleep(interval)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # Final report
    print("\n" + "═" * 50)
    print("  FINAL REPORT")
    print("═" * 50)
    print(stats.summary())
    print()

    if stats.latencies:
        buckets = defaultdict(int)
        for l in stats.latencies:
            bucket = int(l // 20) * 20
            buckets[bucket] += 1
        print("  Latency distribution:")
        max_count = max(buckets.values())
        for b in sorted(buckets)[:15]:
            bar = "█" * int(buckets[b] / max_count * 30)
            print(f"    {b:>4}-{b+20}ms │ {bar} {buckets[b]}")

    print("\n✅ Simulation complete")
    return stats


# ── Setup ─────────────────────────────────────────────────────────────────────

async def get_slot_ids(base_url: str) -> list:
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{base_url}/api/v1/publishers/slots", timeout=5.0)
            slots = resp.json()
            if not slots:
                print("⚠️  No ad slots found. Run the seed endpoint first:")
                print(f"   curl -X POST {base_url}/api/v1/seed/")
                sys.exit(1)
            ids = [s["id"] for s in slots]
            print(f"✅ Found {len(ids)} ad slots")
            return ids
        except Exception as e:
            print(f"❌ Cannot reach backend at {base_url}: {e}")
            print("   Make sure docker-compose is running: docker-compose up")
            sys.exit(1)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="RTB Auction Engine Load Simulator")
    parser.add_argument("--rps",      type=int, default=10,
                        help="Requests per second (default: 10; keep ≤15 for local Docker)")
    parser.add_argument("--duration", type=int, default=60,
                        help="Duration in seconds (default: 60)")
    parser.add_argument("--url",      type=str, default="http://localhost:8000",
                        help="Backend base URL")
    args = parser.parse_args()

    slot_ids = await get_slot_ids(args.url)
    await dispatcher(args.url, args.rps, args.duration, slot_ids)


if __name__ == "__main__":
    asyncio.run(main())