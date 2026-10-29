"""
Exchange Simulator
==================
Plays the exchange for the engine's OpenRTB bidder, so the whole loop can be
watched without a real one: bid request, second-price auction against other
bidders, win or loss notice, and the odd click on a winning ad.

Like real exchanges, it sends loss notices for only some lost auctions. The
rest never hear back, and the bidder's sweeper has to release their budget.

Usage:
    python scripts/simulate_exchange.py --requests 500
    python scripts/simulate_exchange.py --requests 2000 --concurrency 20 --url https://rtb-engine.onrender.com

Run POST /api/v1/seed/ first so there are campaigns to bid.
"""

import argparse
import asyncio
import random
import statistics
import time
import uuid

import httpx

SITES = [
    ("espn.com", "https://www.espn.com/football/match", ["IAB17"]),
    ("bloomberg.com", "https://www.bloomberg.com/markets", ["IAB3"]),
    ("vogue.com", "https://www.vogue.com/fashion", ["IAB18"]),
    ("techcrunch.com", "https://techcrunch.com/ai", ["IAB19"]),
    ("bbc.co.uk", "https://www.bbc.co.uk/news/world", ["IAB12"]),
    ("punchng.com", "https://punchng.com/sports", []),
]
SIZES = [(300, 250), (728, 90), (320, 50), (300, 600), (320, 480)]
COUNTRIES = ["USA", "GBR", "NGA", "DEU", "CAN", "AUS", "FRA", "BRA"]
DEVICES = [2, 2, 4, 4, 5]          # OpenRTB 5.21: PC, phone, tablet


def bid_request() -> dict:
    domain, page, cat = random.choice(SITES)
    w, h = random.choice(SIZES)
    return {
        "id": str(uuid.uuid4()),
        "imp": [{"id": "1", "banner": {"w": w, "h": h, "pos": random.choice([0, 1, 3])},
                 "bidfloor": round(random.uniform(0.05, 0.25), 2), "bidfloorcur": "USD"}],
        "site": {"domain": domain, "page": page, "cat": cat},
        "device": {"devicetype": random.choice(DEVICES), "ua": "Mozilla/5.0",
                   "geo": {"country": random.choice(COUNTRIES)}},
        "user": {"data": [{"segment": [{"id": f"seg{random.randint(1, 40)}"}
                                       for _ in range(random.randint(0, 4))]}]},
        "at": 2, "tmax": 120, "cur": ["USD"],
    }


async def one_auction(client, args, stats):
    request = bid_request()
    start = time.perf_counter()
    r = await client.post(f"{args.url}/api/v1/openrtb/2.5/bid", json=request)
    stats["latency_ms"].append((time.perf_counter() - start) * 1000)
    if r.status_code == 204:
        stats["no_bid"] += 1
        return
    r.raise_for_status()
    bid = r.json()["seatbid"][0]["bid"][0]
    stats["bids"] += 1

    # The other bidders: the best of them, as a CPM in dollars.
    competitor = random.lognormvariate(-0.9, 0.6)
    floor = request["imp"][0]["bidfloor"]
    if bid["price"] > competitor:
        clearing = max(competitor, floor) + 0.01          # second price
        clearing = min(clearing, bid["price"])
        await client.get(bid["nurl"].replace("${AUCTION_PRICE}", f"{clearing:.4f}"))
        stats["won"] += 1
        stats["spend"] += clearing / 1000
        if random.random() < args.ctr:
            await client.get(bid["adm"].split('href="')[1].split('"')[0], follow_redirects=False)
            stats["clicks"] += 1
    else:
        stats["lost"] += 1
        if random.random() < args.loss_notice_rate:
            await client.get(bid["lurl"].replace("${AUCTION_LOSS}", "102"))


async def main(args):
    stats = {"latency_ms": [], "no_bid": 0, "bids": 0, "won": 0, "lost": 0,
             "clicks": 0, "spend": 0.0}
    gate = asyncio.Semaphore(args.concurrency)

    async def guarded(client):
        async with gate:
            await one_auction(client, args, stats)

    async with httpx.AsyncClient(timeout=30) as client:
        await asyncio.gather(*(guarded(client) for _ in range(args.requests)))
        engine_stats = (await client.get(f"{args.url}/api/v1/openrtb/stats")).json()

    lat = sorted(stats["latency_ms"])
    print(f"requests {args.requests}: {stats['bids']} bids, {stats['no_bid']} no-bids")
    print(f"won {stats['won']}, lost {stats['lost']}, clicks {stats['clicks']}, "
          f"spend ${stats['spend']:.4f}")
    print(f"bid latency (client) p50 {statistics.median(lat):.1f}ms "
          f"p95 {lat[int(len(lat) * 0.95)]:.1f}ms")
    print("engine says:", engine_stats)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--requests", type=int, default=500)
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--ctr", type=float, default=0.02, help="chance a won ad is clicked")
    p.add_argument("--loss-notice-rate", type=float, default=0.5,
                   help="share of lost auctions the exchange reports")
    asyncio.run(main(p.parse_args()))
