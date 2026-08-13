"""
Impression Stream Consumer
==========================
A background worker that reads impression events from the Redis Stream
and processes them for analytics and billing.

Run it alongside the main API:
    python scripts/stream_consumer.py

Windows compatible — uses KeyboardInterrupt instead of Unix signals.
"""

import asyncio
import redis.asyncio as aioredis
import platform
import signal
from datetime import datetime, timezone
from collections import defaultdict
import os

# ── Config ────────────────────────────────────────────────────────────────────

REDIS_URL         = os.getenv("REDIS_URL", "redis://localhost:6379")
IMPRESSION_STREAM = "rtb:impressions"
CONSUMER_GROUP    = "analytics-workers"
CONSUMER_NAME     = "worker-1"
BATCH_SIZE        = 100
BLOCK_MS          = 2000


# ── In-memory aggregator ──────────────────────────────────────────────────────

class AnalyticsAggregator:
    def __init__(self):
        self.impressions_by_campaign = defaultdict(int)
        self.spend_by_campaign       = defaultdict(int)
        self.impressions_by_country  = defaultdict(int)
        self.impressions_by_device   = defaultdict(int)
        self.total_processed         = 0
        self.total_spend_micros      = 0
        self.start_time              = datetime.now(timezone.utc)

    def record_impression(self, event: dict):
        campaign_id    = event.get("campaign_id", "unknown")
        charged_cost = int(event.get("charged_cost_micros", 0))
        country        = event.get("country", "unknown")
        device         = event.get("device_type", "unknown")

        self.impressions_by_campaign[campaign_id] += 1
        self.spend_by_campaign[campaign_id]       += charged_cost
        self.impressions_by_country[country]      += 1
        self.impressions_by_device[device]        += 1
        self.total_processed                      += 1
        self.total_spend_micros                   += charged_cost

    def report(self) -> str:
        elapsed = (datetime.now(timezone.utc) - self.start_time).total_seconds()
        lines = [
            f"\n{'═'*52}",
            f"  STREAM CONSUMER — Live Analytics",
            f"{'═'*52}",
            f"  Uptime:            {elapsed:.0f}s",
            f"  Total impressions: {self.total_processed:,}",
            f"  Total spend:       ${self.total_spend_micros/1_000_000:,.4f}",
            f"  Throughput:        {self.total_processed/elapsed:.1f} events/s" if elapsed > 0 else "  Throughput:        —",
            f"",
            f"  Top campaigns by impressions:",
        ]
        top = sorted(self.impressions_by_campaign.items(), key=lambda x: x[1], reverse=True)[:5]
        for campaign_id, count in top:
            spend_micros = self.spend_by_campaign[campaign_id]
            lines.append(
                f"    {campaign_id[:8]}...  {count:>6} imps  "
                f"${spend_micros/1_000_000:.4f} spend"
            )

        if self.impressions_by_country:
            lines.append(f"\n  Impressions by country:")
            max_c = max(self.impressions_by_country.values())
            top_c = sorted(self.impressions_by_country.items(), key=lambda x: x[1], reverse=True)[:5]
            for country, count in top_c:
                bar = "█" * int(count / max_c * 20)
                lines.append(f"    {country:>4}  {bar} {count}")

        if self.impressions_by_device:
            lines.append(f"\n  Impressions by device:")
            for device, count in sorted(self.impressions_by_device.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"    {device:<10}  {count:,}")

        return "\n".join(lines)


# ── Consumer ──────────────────────────────────────────────────────────────────

class ImpressionConsumer:
    def __init__(self):
        self.redis      = aioredis.from_url(REDIS_URL, decode_responses=True)
        self.aggregator = AnalyticsAggregator()
        self.running    = True

    async def setup(self):
        try:
            await self.redis.xgroup_create(
                IMPRESSION_STREAM, CONSUMER_GROUP,
                id="0", mkstream=True,
            )
            print(f"✅ Created consumer group '{CONSUMER_GROUP}'")
        except Exception as e:
            if "BUSYGROUP" in str(e):
                print(f"ℹ️  Consumer group '{CONSUMER_GROUP}' already exists — resuming")
            else:
                raise

    async def _handle_messages(self, messages):
        if not messages:
            return
        for stream_name, entries in messages:
            for msg_id, fields in entries:
                try:
                    self.aggregator.record_impression(fields)
                    await self.redis.xack(IMPRESSION_STREAM, CONSUMER_GROUP, msg_id)
                except Exception as e:
                    print(f"  ⚠️  Failed to process {msg_id}: {e}")

    async def run(self):
        await self.setup()

        # Re-process any unACK'd messages from a previous crash
        pending = await self.redis.xreadgroup(
            CONSUMER_GROUP, CONSUMER_NAME,
            {IMPRESSION_STREAM: "0"}, count=BATCH_SIZE,
        )
        if pending:
            print(f"⚠️  Reprocessing pending messages...")
            await self._handle_messages(pending)

        print(f"\n👂 Listening on stream '{IMPRESSION_STREAM}'...")
        print(f"   Group: {CONSUMER_GROUP} | Consumer: {CONSUMER_NAME}")
        print(f"   Batch size: {BATCH_SIZE} | Block: {BLOCK_MS}ms\n")

        report_interval = 10
        last_report     = asyncio.get_event_loop().time()

        while self.running:
            try:
                messages = await self.redis.xreadgroup(
                    CONSUMER_GROUP, CONSUMER_NAME,
                    {IMPRESSION_STREAM: ">"},
                    count=BATCH_SIZE,
                    block=BLOCK_MS,
                )
                await self._handle_messages(messages)

                now = asyncio.get_event_loop().time()
                if now - last_report >= report_interval:
                    print(self.aggregator.report())
                    last_report = now

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.running:
                    print(f"  ⚠️  Consumer error: {e}")
                    await asyncio.sleep(1)

        await self.redis.aclose()
        print("\n🔌 Consumer shut down cleanly")
        if self.aggregator.total_processed > 0:
            print(self.aggregator.report())

    def stop(self):
        self.running = False


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    consumer = ImpressionConsumer()

    # Unix: use signal handlers for clean shutdown
    # Windows: KeyboardInterrupt handles it
    if platform.system() != "Windows":
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, consumer.stop)

    print("🔄 RTB Impression Stream Consumer")
    print(f"   Redis:    {REDIS_URL}")
    print(f"   Platform: {platform.system()}")
    print("   Press Ctrl+C to stop\n")

    try:
        await consumer.run()
    except KeyboardInterrupt:
        consumer.stop()
        print("\n👋 Stopped by user")


if __name__ == "__main__":
    asyncio.run(main())
