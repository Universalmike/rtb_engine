"""
Seed endpoint: creates realistic demo data so the dashboard has something to show.
Generates advertisers, campaigns, publishers, ad slots, and runs 200 simulated auctions.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from faker import Faker
from datetime import datetime, timedelta
import random
import json

from app.core.database import get_db, reset_schema
from app.models.models import (
    Advertiser, Campaign, CampaignStatus, AuctionType,
    Publisher, AdSlot, AdCreative, DeviceType,
    BidRecord, AuctionResult, Impression, Click
)
from app.schemas.schemas import BidRequest
from app.services.auction_engine import AuctionEngine
from app.ml.artifacts import load_empirical_ctr
from app.ml.mappings import SLOT_NAME_TO_BANNER_POS
from app.ml.simulation import lookup_ctr

router = APIRouter()
fake = Faker()

CATEGORIES = ["tech", "finance", "sports", "entertainment", "news", "fashion"]
COUNTRIES = ["US", "GB", "NG", "DE", "CA", "AU", "FR", "BR"]
DEVICES = [DeviceType.DESKTOP, DeviceType.MOBILE, DeviceType.TABLET]

# Number of auctions simulated per seed. Raised from 60 so the A/B comparison
# accumulates enough clicks to be statistically legible on the dashboard. On a
# long-running host (Render) this finishes comfortably; on a serverless function
# with a short timeout, drop it back down.
SEED_AUCTIONS = 400

def _value_per_click_millicents(max_cpm_cents: int, baseline_ctr: float) -> int:
    """Click value (in millicents) that makes EV(baseline) == max_cpm, jittered.

    Keeps mean treatment spend ≈ control spend so the A/B measures better
    allocation of budget, not a systematic bid multiplier. Millicents (not cents)
    because Avazu's ~16% CTR makes the fair value sub-one-cent — integer cents
    would round every campaign to the same 1 and defeat the normalization.
    """
    base = max_cpm_cents / baseline_ctr  # = max_cpm/(baseline*1000) * 1000
    return max(1, round(base * random.uniform(0.7, 1.3)))


@router.post("/")
async def seed_database(db: AsyncSession = Depends(get_db)):
    """
    Wipe and reseed with realistic demo data.

    This endpoint is public and visitors will click it repeatedly, so it must
    be idempotent — it clears existing rows first rather than piling a fresh
    dataset on top of the old one every time.
    """
    # Drop and recreate every table so schema changes (new columns) take effect.
    # The demo data is disposable, so this replaces the old row-by-row wipe.
    await reset_schema()

    # Held-out empirical CTRs drive realistic click simulation below.
    empirical = load_empirical_ctr()
    baseline_ctr = empirical.get("__baseline__", 0.163)

    # ── Advertisers ──────────────────────────────────────────────────────────
    advertiser_names = [
        "TechBrand Inc", "FinFlow Capital", "SportsPulse", "StyleHive",
        "NewsDesk Media", "EduPath Online", "HealthFirst Corp", "AutoDrive Co"
    ]
    advertisers = []
    for name in advertiser_names:
        adv = Advertiser(
            name=name,
            domain=fake.domain_name(),
        )
        db.add(adv)
        advertisers.append(adv)
    await db.flush()

    # ── Publishers ───────────────────────────────────────────────────────────
    publisher_data = [
        ("TechCrunch Clone", "techcrunchclone.com", "tech"),
        ("Sports Daily", "sportsdailynews.com", "sports"),
        ("Finance Weekly", "financeweekly.io", "finance"),
        ("Entertainment Hub", "enthub.net", "entertainment"),
        ("News Portal", "newsportal.co", "news"),
    ]
    publishers = []
    slots = []
    for pub_name, domain, category in publisher_data:
        pub = Publisher(name=pub_name, domain=domain, category=category)
        db.add(pub)
        publishers.append(pub)
    await db.flush()

    # ── Ad Slots ─────────────────────────────────────────────────────────────
    slot_configs = [
        ("Leaderboard 728x90", 728, 90, DeviceType.DESKTOP, 15),
        ("Medium Rectangle 300x250", 300, 250, DeviceType.DESKTOP, 20),
        ("Mobile Banner 320x50", 320, 50, DeviceType.MOBILE, 10),
        ("Half Page 300x600", 300, 600, DeviceType.DESKTOP, 25),
        ("Mobile Interstitial 320x480", 320, 480, DeviceType.MOBILE, 18),
    ]
    slot_context = {}  # slot.id -> (publisher_category, banner_pos)
    for pub in publishers:
        for slot_name, w, h, device, floor in slot_configs:
            slot = AdSlot(
                publisher_id=pub.id,
                name=slot_name,
                width=w, height=h,
                floor_price_cents=floor,
                device_type=device,
                banner_pos=SLOT_NAME_TO_BANNER_POS.get(slot_name, 0),
            )
            db.add(slot)
            slots.append(slot)
    await db.flush()
    for pub in publishers:
        for slot in slots:
            if slot.publisher_id == pub.id:
                slot_context[slot.id] = (pub.category,
                                         SLOT_NAME_TO_BANNER_POS.get(slot.name, 0))

    # ── Campaigns ────────────────────────────────────────────────────────────
    campaigns = []
    for adv in advertisers:
        for i in range(random.randint(1, 3)):
            daily_budget = random.randint(5000, 50000)   # $50–$500/day
            campaign_max_cpm = random.randint(20, 150)   # $0.20–$1.50 CPM
            campaign = Campaign(
                advertiser_id=adv.id,
                name=f"{adv.name} — Campaign {i+1}",
                status=CampaignStatus.ACTIVE,
                daily_budget_cents=daily_budget,
                total_budget_cents=daily_budget * 30,
                max_cpm_cents=campaign_max_cpm,
                value_per_click_millicents=_value_per_click_millicents(
                    campaign_max_cpm, baseline_ctr),
                auction_type=AuctionType.SECOND_PRICE,
                target_countries=json.dumps(random.sample(COUNTRIES, k=random.randint(1, 4))),
                target_devices=json.dumps([d.value for d in random.sample(DEVICES, k=random.randint(1, 2))]),
                target_categories=json.dumps(random.sample(CATEGORIES, k=2)),
                start_date=datetime.utcnow() - timedelta(days=7),
                end_date=datetime.utcnow() + timedelta(days=30),
            )
            db.add(campaign)
            campaigns.append(campaign)

    # Run-of-network campaigns: no country or device targeting, so they bid on
    # every request. Without these, targeting is assigned randomly and whole
    # country/device combinations can end up with no demand at all — a visitor
    # picking one gets "no fill" and assumes the engine is broken. Real DSPs
    # carry RON campaigns for the same reason. Their CPM sits at the low end so
    # they backstop the auction without winning the interesting ones.
    for adv in random.sample(advertisers, k=2):
        ron_max_cpm = random.randint(26, 40)
        ron = Campaign(
            advertiser_id=adv.id,
            name=f"{adv.name} — Run of Network",
            status=CampaignStatus.ACTIVE,
            daily_budget_cents=200000,       # deliberately large: an exhausted
            total_budget_cents=200000 * 30,  # RON campaign reopens the gap
            max_cpm_cents=ron_max_cpm,
            value_per_click_millicents=_value_per_click_millicents(
                ron_max_cpm, baseline_ctr),
            auction_type=AuctionType.SECOND_PRICE,
            target_countries=json.dumps([]),
            target_devices=json.dumps([]),
            target_categories=json.dumps([]),
            start_date=datetime.utcnow() - timedelta(days=7),
            end_date=datetime.utcnow() + timedelta(days=30),
        )
        db.add(ron)
        campaigns.append(ron)
    await db.flush()

    # ── Creatives ────────────────────────────────────────────────────────────
    for campaign in campaigns:
        creative = AdCreative(
            campaign_id=campaign.id,
            name=f"{campaign.name} — Banner",
            creative_type="banner",
            width=728, height=90,
            click_url=f"https://{fake.domain_name()}/landing",
            asset_url=f"https://picsum.photos/728/90?random={random.randint(1,999)}",
        )
        db.add(creative)
    await db.flush()

    # ── Simulate auctions ────────────────────────────────────────────────────
    engine = AuctionEngine(db)
    auction_count = 0
    click_count = 0
    for _ in range(SEED_AUCTIONS):
        slot = random.choice(slots)
        country = random.choice(COUNTRIES)
        device = random.choice(DEVICES)

        bid_req = BidRequest(
            ad_slot_id=slot.id,
            country=country,
            device_type=device,
            page_url=f"https://{fake.domain_name()}/article/{fake.slug()}",
            user_agent=fake.user_agent(),
        )
        try:
            resp = await engine.run_auction(bid_req)
            auction_count += 1
        except Exception as e:
            print(f"Auction failed: {e}")
            continue

        if not resp.had_fill:
            continue

        # Draw a click from the held-out empirical CTR for this segment. Ground
        # truth the served model never trained on, so measured A/B lift is real.
        pub_category, banner_pos = slot_context[slot.id]
        ctr = lookup_ctr(empirical, device.value, pub_category, banner_pos,
                         datetime.utcnow().hour)
        if random.random() < ctr:
            imp = (await db.execute(
                select(Impression).where(Impression.auction_id == resp.auction_id)
            )).scalar_one_or_none()
            if imp:
                db.add(Click(impression_id=imp.id, campaign_id=imp.campaign_id))
                click_count += 1
    await db.flush()

    return {
        "message": "Database seeded successfully",
        "advertisers": len(advertisers),
        "publishers": len(publishers),
        "ad_slots": len(slots),
        "campaigns": len(campaigns),
        "auctions_simulated": auction_count,
        "clicks_simulated": click_count,
    }
