"""
Budget Reset Utility
====================
Resets daily spend on all campaigns so you can keep running demos
without campaigns going EXHAUSTED mid-presentation.

Usage:
    python scripts/reset_budgets.py
    python scripts/reset_budgets.py --url http://your-backend.railway.app
"""

import asyncio
import httpx
import argparse


async def reset(base_url: str):
    async with httpx.AsyncClient() as client:
        # Get all campaigns
        resp = await client.get(f"{base_url}/api/v1/campaigns/", timeout=5.0)
        campaigns = resp.json()
        print(f"Found {len(campaigns)} campaigns\n")

        reactivated = 0
        for c in campaigns:
            if c["status"] in ("exhausted", "paused"):
                r = await client.patch(
                    f"{base_url}/api/v1/campaigns/{c['id']}/activate",
                    timeout=5.0
                )
                if r.status_code == 200:
                    print(f"  ✅ Reactivated: {c['name'][:50]}")
                    reactivated += 1

        print(f"\n✅ Reset complete — {reactivated} campaigns reactivated")
        print(f"   {len(campaigns) - reactivated} campaigns were already active")


async def main():
    parser = argparse.ArgumentParser(description="Reset campaign budgets for demo")
    parser.add_argument("--url", type=str, default="http://localhost:8000", help="Backend URL")
    args = parser.parse_args()
    await reset(args.url)


if __name__ == "__main__":
    asyncio.run(main())
