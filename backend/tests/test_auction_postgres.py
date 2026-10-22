"""The auction against a real Postgres: the write executes, and money adds up.

Everything else in this suite runs on session doubles, which check the shape
of a statement but not whether Postgres accepts it -- and the single-statement
write is precisely the kind of SQL whose type inference and CTE semantics only
the database can confirm. Set TEST_DATABASE_URL to run these; the tables in
that database are dropped and recreated, so never point it at real data.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.database import Base
from app.models.models import (
    AdCreative, AdSlot, Advertiser, AuctionResult, AuctionType, BidRecord,
    Campaign, CampaignStatus, DeviceType, Impression, Publisher,
)
from app.schemas.schemas import BidRequest
from app.services.accounting import MICROS_PER_CENT
from app.services.auction_engine import AuctionEngine, clear_slot_cache

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="TEST_DATABASE_URL not set")


def run(scenario):
    """Run scenario(sessionmaker) on a freshly created schema."""
    async def main():
        engine = create_async_engine(DATABASE_URL, pool_size=5, max_overflow=5)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
            clear_slot_cache()
            sessions = async_sessionmaker(
                engine.execution_options(isolation_level="AUTOCOMMIT"),
                class_=AsyncSession, expire_on_commit=False)
            return await scenario(sessions)
        finally:
            clear_slot_cache()
            await engine.dispose()
    return asyncio.run(main())


async def seed(sessions, *, campaigns=1, daily_budget_cents=1_000, max_cpm=150):
    async with sessions() as s:
        advertiser = Advertiser(name="Advertiser", domain="adv.example")
        publisher = Publisher(name="Publisher", domain="pub.example", category="tech")
        s.add_all([advertiser, publisher])
        await s.flush()
        slot = AdSlot(publisher_id=publisher.id, name="Slot", width=300,
                      height=250, floor_price_cents=10,
                      device_type=DeviceType.DESKTOP)
        s.add(slot)
        for i in range(campaigns):
            c = Campaign(
                advertiser_id=advertiser.id, name=f"Campaign {i}",
                status=CampaignStatus.ACTIVE,
                daily_budget_cents=daily_budget_cents, total_budget_cents=100_000,
                max_cpm_cents=max_cpm - i, value_per_click_millicents=1_000,
                auction_type=AuctionType.SECOND_PRICE,
                target_countries=json.dumps([]), target_devices=json.dumps([]),
                target_categories=json.dumps([]),
                start_date=datetime.utcnow() - timedelta(days=1),
            )
            s.add(c)
            await s.flush()
            # One creative that fits and one that does not: only the first
            # may be loaded, or chosen.
            s.add_all([
                AdCreative(campaign_id=c.id, name="fits", width=300, height=250,
                           click_url="https://adv.example", asset_url="https://adv.example/a.png"),
                AdCreative(campaign_id=c.id, name="wrong size", width=728, height=90,
                           click_url="https://adv.example", asset_url="https://adv.example/b.png"),
            ])
        await s.flush()
        return slot.id


async def auction(sessions, slot_id):
    async with sessions() as s:
        return await AuctionEngine(s).run_auction(BidRequest(
            ad_slot_id=slot_id, country="US", device_type="desktop",
            page_url="https://pub.example/article"))


async def count(s, model, *where):
    return (await s.execute(select(func.count()).select_from(model).where(*where))).scalar()


def test_a_filled_auction_records_exactly_what_it_charged():
    async def scenario(sessions):
        slot_id = await seed(sessions, campaigns=3)
        response = await auction(sessions, slot_id)

        async with sessions() as s:
            result = (await s.execute(select(AuctionResult))).scalar_one()
            impression = (await s.execute(select(Impression))).scalar_one()
            bids = (await s.execute(
                select(BidRecord).order_by(BidRecord.bid_price_cents.desc())
            )).scalars().all()
            winner = await s.get(Campaign, response.winning_campaign_id)
            creative = await s.get(AdCreative, response.winning_creative_id)
        return response, result, impression, bids, winner, creative

    response, result, impression, bids, winner, creative = run(scenario)

    assert response.had_fill and response.num_bidders == 3
    # Second price: the runner-up bid 149, so the winner pays 150 at most.
    assert response.highest_bid_cents == 150
    assert response.clearing_price_cents == 150
    assert response.charged_cost_micros == 1_500
    assert result.charged_cost_micros == impression.charged_cost_micros == 1_500
    assert result.winning_campaign_id == impression.campaign_id == winner.id
    assert winner.total_spent_micros == winner.spent_today_micros == 1_500
    assert [b.bid_price_cents for b in bids] == [150, 149, 148]
    assert [b.is_winner for b in bids] == [True, False, False]
    assert {b.auction_id for b in bids} == {response.auction_id}
    assert creative.width == 300 and creative.height == 250


def test_one_session_serves_auctions_on_slots_of_different_sizes():
    """The seeder runs every auction through one session.

    Eligibility loads only the creatives that fit the slot. In a session that
    already holds the campaign, a collection loaded for one slot size must not
    be what the next auction, on another size, sees -- or it finds no fitting
    creative and quietly no-fills.
    """
    async def scenario(sessions):
        slot_id = await seed(sessions)
        async with sessions() as s:
            first = await s.get(AdSlot, slot_id)
            other = AdSlot(publisher_id=first.publisher_id, name="Leaderboard",
                           width=728, height=90, floor_price_cents=10,
                           device_type=DeviceType.DESKTOP)
            s.add(other)
            await s.flush()
            other_id = other.id
        async with sessions() as s:
            # The seeder holds its campaigns for the whole run. The identity
            # map is weak, so without these references each auction would
            # load the campaigns afresh and the bug could not show.
            held = (await s.execute(select(Campaign))).scalars().all()
            engine = AuctionEngine(s)
            results = []
            for sid in (slot_id, other_id, slot_id):
                results.append(await engine.run_auction(BidRequest(
                    ad_slot_id=sid, country="US", device_type="desktop")))
        return results

    results = run(scenario)
    assert [r.had_fill for r in results] == [True, True, True], \
        [r.excluded for r in results]


def test_a_refused_charge_writes_nothing():
    """The race the retry loop handles: budget gone between read and charge."""
    async def scenario(sessions):
        slot_id = await seed(sessions)
        async with sessions() as s:
            engine = AuctionEngine(s)
            slot = await engine._get_ad_slot(slot_id)
            [campaign] = await engine._query_solvent_campaigns(slot)
            # Another auction spends the whole lifetime budget first.
            await s.execute(
                update(Campaign)
                .where(Campaign.id == campaign.id)
                .values(total_spent_micros=Campaign.total_budget_cents * MICROS_PER_CENT)
                .execution_options(synchronize_session=False))

            bids = [{"campaign": campaign, "bid_cents": 150}]
            result = {"winner": campaign, "highest_bid_cents": 150,
                      "clearing_price_cents": 11}
            records = engine._auction_records(
                "lost-race", slot, BidRequest(ad_slot_id=slot_id), bids, result,
                campaign.creatives[0], 110, "control")
            charged = await engine._reserve_budget(campaign, 110, records)

            counts = [await count(s, m) for m in (AuctionResult, Impression, BidRecord)]
        return charged, counts

    charged, counts = run(scenario)
    assert charged is False
    assert counts == [0, 0, 0]


def test_concurrent_auctions_never_overspend_and_every_charge_is_recorded():
    async def scenario(sessions):
        # One cent a day buys a few dozen impressions at these prices.
        slot_id = await seed(sessions, daily_budget_cents=1, max_cpm=50)
        responses = await asyncio.gather(*(auction(sessions, slot_id) for _ in range(150)))
        async with sessions() as s:
            campaign = (await s.execute(select(Campaign))).scalar_one()
            impressions = await count(s, Impression)
            charged = (await s.execute(
                select(func.coalesce(func.sum(Impression.charged_cost_micros), 0))
            )).scalar()
            filled_results = await count(s, AuctionResult, AuctionResult.had_fill)
        return responses, campaign, impressions, charged, filled_results

    responses, campaign, impressions, charged, filled_results = run(scenario)
    filled = [r for r in responses if r.had_fill]

    assert filled, "the budget should buy at least one impression"
    assert len(filled) < len(responses), "the budget should run out"
    assert campaign.spent_today_micros <= 1 * MICROS_PER_CENT
    assert campaign.spent_today_micros == charged == sum(r.charged_cost_micros for r in filled)
    assert impressions == filled_results == len(filled)
    assert campaign.status == CampaignStatus.EXHAUSTED
