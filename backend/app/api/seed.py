"""
Seed endpoint: creates realistic demo data so the dashboard has something to show.
Generates advertisers, campaigns, publishers, ad slots, and runs 200 simulated auctions.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from faker import Faker
from datetime import datetime, timedelta
import random
import json

from app.core.database import get_db
from app.models.models import (
    Advertiser, Campaign, CampaignStatus, AuctionType,
    Publisher, AdSlot, AdCreative, DeviceType
)
from app.schemas.schemas import BidRequest
from app.services.auction_engine import AuctionEngine

router = APIRouter()
fake = Faker()

CATEGORIES = ["tech", "finance", "sports", "entertainment", "news", "fashion"]
COUNTRIES = ["US", "GB", "NG", "DE", "CA", "AU", "FR", "BR"]
DEVICES = [DeviceType.DESKTOP, DeviceType.MOBILE, DeviceType.TABLET]


@router.post("/")
async def seed_database(db: AsyncSession = Depends(get_db)):
    """Wipe and reseed with realistic demo data."""

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
    for pub in publishers:
        for slot_name, w, h, device, floor in slot_configs:
            slot = AdSlot(
                publisher_id=pub.id,
                name=slot_name,
                width=w, height=h,
                floor_price_cents=floor,
                device_type=device,
            )
            db.add(slot)
            slots.append(slot)
    await db.flush()

    # ── Campaigns ────────────────────────────────────────────────────────────
    campaigns = []
    for adv in advertisers:
        for i in range(random.randint(1, 3)):
            daily_budget = random.randint(5000, 50000)   # $50–$500/day
            campaign = Campaign(
                advertiser_id=adv.id,
                name=f"{adv.name} — Campaign {i+1}",
                status=CampaignStatus.ACTIVE,
                daily_budget_cents=daily_budget,
                total_budget_cents=daily_budget * 30,
                max_cpm_cents=random.randint(20, 150),   # $0.20–$1.50 CPM
                auction_type=AuctionType.SECOND_PRICE,
                target_countries=json.dumps(random.sample(COUNTRIES, k=random.randint(1, 4))),
                target_devices=json.dumps([d.value for d in random.sample(DEVICES, k=random.randint(1, 2))]),
                target_categories=json.dumps(random.sample(CATEGORIES, k=2)),
                start_date=datetime.utcnow() - timedelta(days=7),
                end_date=datetime.utcnow() + timedelta(days=30),
            )
            db.add(campaign)
            campaigns.append(campaign)
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
    for _ in range(200):
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
            await engine.run_auction(bid_req)
            auction_count += 1
        except Exception as e:
            print(f"Auction failed: {e}")

    return {
        "message": "Database seeded successfully",
        "advertisers": len(advertisers),
        "publishers": len(publishers),
        "ad_slots": len(slots),
        "campaigns": len(campaigns),
        "auctions_simulated": auction_count,
    }
