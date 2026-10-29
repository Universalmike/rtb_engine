"""The OpenRTB bidder: mapping, then money, against a real Postgres.

The bid path runs on autocommit sessions, as in production. Settlement runs
in real transactions, also as in production: its correctness rests on row
locks held until commit, which an autocommit session would release at once.
"""
import asyncio
import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import analytics, openrtb as openrtb_api
from app.core.database import Base
from app.models.models import (
    AdCreative, AdSlot, Advertiser, AuctionResult, AuctionType, Campaign, CampaignStatus,
    Click, DeviceType, ExchangeBid, Impression, Publisher,
)
from app.schemas import openrtb as ortb
from app.schemas.schemas import BidRequest as HouseBidRequest
from app.services import openrtb as service
from app.services.accounting import MICROS_PER_CENT
from app.services.auction_engine import AuctionEngine, clear_slot_cache
from app.services.openrtb import Bidder
from app.services.pacing import ahead_of_pace, allowed_spend_micros

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL not set")
BASE = "http://bidder.test/"


# ── Mapping (no database) ─────────────────────────────────────────────────────

def test_countries_arrive_as_alpha3_and_unknown_ones_match_nothing():
    assert service.country_alpha2("NGA") == "NG"
    assert service.country_alpha2("usa") == "US"
    assert service.country_alpha2("GB") == "GB"
    assert service.country_alpha2("XYZ") == ""
    assert service.country_alpha2(None) == ""


def test_device_types_follow_the_spec_and_ctv_is_not_bid_on():
    d = ortb.Device
    assert service.device_of(d(devicetype=2)) == "desktop"
    assert service.device_of(d(devicetype=4)) == "mobile"
    assert service.device_of(d(devicetype=5)) == "tablet"
    assert service.device_of(d(devicetype=3)) is None
    assert service.device_of(d(ua="Mozilla/5.0 (iPad; CPU OS 17_0)")) == "tablet"
    assert service.device_of(None) == "desktop"


def test_category_comes_from_iab_codes_then_from_the_url():
    assert service.category_of(ortb.Site(cat=["IAB17-44"])) == ("sports", "iab")
    assert service.category_of(ortb.Site(cat=["IAB2"], page="https://www.espn.com/nba"))[0] == "sports"
    assert service.category_of(None) == (None, "unknown")


def test_prices_are_exact_and_unsubstituted_macros_are_refused():
    assert service.price_to_micros("0.3") == 300
    assert service.price_to_micros("1.2345") == 1235       # half up
    assert service.price_to_micros("2") == 2000
    for bad in ("${AUCTION_PRICE}", "-1", "nan", "inf", ""):
        with pytest.raises(ValueError):
            service.price_to_micros(bad)


def test_a_floor_is_rounded_up_so_a_bid_at_it_still_clears_it():
    assert service.floor_cents(ortb.Imp(id="1", bidfloor=0.123)) == 13
    assert service.floor_cents(ortb.Imp(id="1", bidfloor=0.5)) == 50
    assert service.floor_cents(ortb.Imp(id="1")) == 0


def test_even_pacing_holds_spend_to_the_share_of_the_day_gone():
    c = SimpleNamespace(daily_budget_micros=1_000_000, pacing="even",
                        effective_spent_today_micros=0, reserved_micros=0)
    six_am = datetime(2026, 10, 30, 6, 0, tzinfo=timezone.utc)
    assert allowed_spend_micros(c, six_am) == 300_000          # 25% + 5% headroom
    c.effective_spent_today_micros = 290_000
    assert not ahead_of_pace(c, 10_000, six_am)
    assert ahead_of_pace(c, 10_001, six_am)
    c.reserved_micros = 10_000                                  # in flight counts
    assert ahead_of_pace(c, 1, six_am)
    c.pacing = "asap"
    assert not ahead_of_pace(c, 1, six_am)


# ── Against Postgres ──────────────────────────────────────────────────────────

def run(scenario):
    async def main():
        engine = create_async_engine(DATABASE_URL, pool_size=10, max_overflow=20)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
            clear_slot_cache()
            fast = async_sessionmaker(engine.execution_options(isolation_level="AUTOCOMMIT"),
                                      class_=AsyncSession, expire_on_commit=False)
            tx = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            return await scenario(fast, tx)
        finally:
            clear_slot_cache()
            await engine.dispose()
    return asyncio.run(main())


async def seed(fast, *, daily_budget_cents=10_000, max_cpm=150, pacing="asap",
               advertiser_domain="adv.example"):
    async with fast() as s:
        adv = Advertiser(name="Advertiser", domain=advertiser_domain)
        s.add(adv)
        await s.flush()
        c = Campaign(advertiser_id=adv.id, name="RON", status=CampaignStatus.ACTIVE,
                     daily_budget_cents=daily_budget_cents, total_budget_cents=1_000_000,
                     max_cpm_cents=max_cpm, value_per_click_millicents=1_000,
                     auction_type=AuctionType.SECOND_PRICE, pacing=pacing,
                     target_countries="[]", target_devices="[]", target_categories="[]",
                     start_date=datetime.utcnow() - timedelta(days=1))
        s.add(c)
        await s.flush()
        s.add(AdCreative(campaign_id=c.id, name="banner", width=300, height=250,
                         click_url="https://adv.example/landing",
                         asset_url="https://adv.example/a.png"))
        await s.flush()
        return c.id


def request(**overrides):
    body = {
        "id": "req-1",
        "imp": [{"id": "1", "banner": {"w": 300, "h": 250, "pos": 1}, "bidfloor": 0.1}],
        "site": {"domain": "sportsnews.example", "page": "https://sportsnews.example/match",
                 "cat": ["IAB17"]},
        "device": {"ua": "Mozilla/5.0", "devicetype": 2, "geo": {"country": "NGA"}},
        "user": {"data": [{"segment": [{"id": "s1"}, {"id": "s2"}]}]},
        "at": 2, "cur": ["USD"],
    }
    body.update(overrides)
    return ortb.BidRequest.model_validate(body)


async def bid(fast, req=None):
    async with fast() as s:
        return await Bidder(s).bid(req or request(), BASE)


async def settle(tx, method, *args):
    async with tx() as s:
        outcome = await getattr(Bidder(s), method)(*args)
        await s.commit()
        return outcome


async def campaign(fast, campaign_id):
    async with fast() as s:
        return await s.get(Campaign, campaign_id)


@pytest.fixture
def control(monkeypatch):
    """Pin the A/B arm: the control bid is the max CPM, which tests can predict."""
    monkeypatch.setattr("app.services.openrtb.random.choice", lambda arms: "control")


@needs_db
def test_a_bid_reserves_its_cost_and_logs_its_features(control):
    async def scenario(fast, tx):
        cid = await seed(fast)
        response = await bid(fast)
        async with fast() as s:
            logged = (await s.execute(select(ExchangeBid))).scalar_one()
        return response, logged, await campaign(fast, cid)

    response, logged, c = run(scenario)
    [b] = response.seatbid[0].bid
    assert b.price == 1.50 and b.impid == "1" and (b.w, b.h) == (300, 250)
    assert b.adomain == ["adv.example"]
    assert b.nurl.endswith(f"/api/v1/openrtb/win/{b.id}?price=${{AUCTION_PRICE}}")
    assert f"/api/v1/openrtb/click/{b.id}" in b.adm
    assert logged.status == "pending" and logged.reserved_micros == 1_500
    assert c.reserved_micros == 1_500 and c.total_spent_micros == 0
    features = json.loads(logged.features)
    assert features["country"] == "NG" and features["category"] == "sports"
    assert features["user_segments"] == ["s1", "s2"]


@needs_db
def test_a_win_charges_the_clearing_price_once_and_records_the_impression(control):
    async def scenario(fast, tx):
        cid = await seed(fast)
        b = (await bid(fast)).seatbid[0].bid[0]
        first = await settle(tx, "settle_win", b.id, service.price_to_micros("0.8"))
        again = await settle(tx, "settle_win", b.id, service.price_to_micros("0.8"))
        async with fast() as s:
            imp = (await s.execute(select(Impression))).scalar_one()
            result = (await s.execute(select(AuctionResult))).scalar_one()
        return first, again, imp, result, await campaign(fast, cid)

    first, again, imp, result, c = run(scenario)
    assert (first, again) == ("charged", "duplicate")
    assert c.total_spent_micros == c.spent_today_micros == 800
    assert c.reserved_micros == 0
    assert imp.charged_cost_micros == 800 and imp.source == "openrtb" and imp.ad_slot_id is None
    assert result.source == "openrtb" and result.charged_cost_micros == 800


@needs_db
def test_concurrent_duplicate_win_notices_charge_once(control):
    async def scenario(fast, tx):
        cid = await seed(fast)
        b = (await bid(fast)).seatbid[0].bid[0]
        outcomes = await asyncio.gather(*(settle(tx, "settle_win", b.id, 900) for _ in range(8)))
        async with fast() as s:
            impressions = (await s.execute(select(func.count()).select_from(Impression))).scalar()
        return outcomes, impressions, await campaign(fast, cid)

    outcomes, impressions, c = run(scenario)
    assert sorted(outcomes) == ["charged"] + ["duplicate"] * 7
    assert impressions == 1 and c.total_spent_micros == 900 and c.reserved_micros == 0


@needs_db
def test_an_exchange_never_charges_above_the_bid(control):
    async def scenario(fast, tx):
        cid = await seed(fast)
        b = (await bid(fast)).seatbid[0].bid[0]
        await settle(tx, "settle_win", b.id, service.price_to_micros("9.99"))
        return await campaign(fast, cid)

    assert run(scenario).total_spent_micros == 1_500


@needs_db
def test_bids_in_flight_can_never_add_up_to_more_than_the_budget(control):
    """One cent a day, 0.5 cents per impression at the max bid: two fit."""
    async def scenario(fast, tx):
        cid = await seed(fast, daily_budget_cents=1, max_cpm=500)
        responses = await asyncio.gather(*(bid(fast, request(id=f"r{i}")) for i in range(30)))
        won = [r.seatbid[0].bid[0] for r in responses if r]
        for b in won:   # and every one of them wins, at its full bid
            await settle(tx, "settle_win", b.id, service.price_to_micros(str(b.price)))
        return len(won), await campaign(fast, cid)

    bids, c = run(scenario)
    assert bids == 2
    assert c.spent_today_micros == 1 * MICROS_PER_CENT
    assert c.reserved_micros == 0


@needs_db
def test_house_auctions_cannot_spend_what_exchange_bids_hold(control):
    async def scenario(fast, tx):
        cid = await seed(fast, daily_budget_cents=1, max_cpm=500)
        await asyncio.gather(*(bid(fast, request(id=f"r{i}")) for i in range(2)))
        async with fast() as s:
            pub = Publisher(name="P", domain="p.example", category="sports")
            s.add(pub)
            await s.flush()
            slot = AdSlot(publisher_id=pub.id, name="MR", width=300, height=250,
                          floor_price_cents=10, device_type=DeviceType.DESKTOP)
            s.add(slot)
            await s.flush()
            house = await AuctionEngine(s, simulate_clicks=False).run_auction(
                HouseBidRequest(ad_slot_id=slot.id, country="NG", device_type="desktop"))
        return house, await campaign(fast, cid)

    house, c = run(scenario)
    assert not house.had_fill
    assert c.reserved_micros == 1 * MICROS_PER_CENT and c.total_spent_micros == 0


@needs_db
def test_a_loss_releases_the_reservation_and_only_once(control):
    async def scenario(fast, tx):
        cid = await seed(fast)
        b = (await bid(fast)).seatbid[0].bid[0]
        outcomes = [await settle(tx, "settle_loss", b.id, 102),
                    await settle(tx, "settle_loss", b.id, 102)]
        return outcomes, await campaign(fast, cid)

    outcomes, c = run(scenario)
    assert outcomes == ["released", "duplicate"]
    assert c.reserved_micros == 0 and c.total_spent_micros == 0


@needs_db
def test_silent_bids_expire_and_a_late_win_is_still_billed(control):
    async def scenario(fast, tx):
        cid = await seed(fast)
        b = (await bid(fast)).seatbid[0].bid[0]
        async with fast() as s:
            await s.execute(update(ExchangeBid).values(
                created_at=datetime.utcnow() - timedelta(minutes=5)))
        expired = await settle(tx, "expire_stale", 60)
        after_expiry = await campaign(fast, cid)
        late = await settle(tx, "settle_win", b.id, 700)
        return expired, after_expiry, late, await campaign(fast, cid)

    expired, after_expiry, late, c = run(scenario)
    assert expired == 1 and after_expiry.reserved_micros == 0
    assert late == "late"
    assert c.total_spent_micros == 700 and c.reserved_micros == 0


@needs_db
def test_a_click_is_counted_once_even_when_it_beats_the_win_notice(control):
    async def scenario(fast, tx):
        await seed(fast)
        b = (await bid(fast)).seatbid[0].bid[0]
        destination = await settle(tx, "record_click", b.id)     # before the win
        await settle(tx, "settle_win", b.id, 500)
        await settle(tx, "record_click", b.id)                   # and again after
        async with fast() as s:
            clicks = (await s.execute(select(Click))).scalars().all()
            imp = (await s.execute(select(Impression))).scalar_one()
        return destination, clicks, imp

    destination, clicks, imp = run(scenario)
    assert destination == "https://adv.example/landing"
    assert [c.impression_id for c in clicks] == [imp.id]


@needs_db
def test_requests_the_bidder_must_decline(control):
    async def scenario(fast, tx):
        await seed(fast)
        declines = {
            "blocked advertiser": request(badv=["adv.example"]),
            "unsafe content": request(site={"domain": "x.example", "cat": ["IAB25-3"]}),
            "connected tv": request(device={"devicetype": 3}),
            "other currency": request(cur=["EUR"]),
            "floor above max bid": request(imp=[{"id": "1", "banner": {"w": 300, "h": 250},
                                                 "bidfloor": 5.0}]),
            "no matching creative": request(imp=[{"id": "1", "banner": {"w": 728, "h": 90}}]),
            "video only": request(imp=[{"id": "1", "video": {"w": 640, "h": 480}}]),
        }
        out = {name: await bid(fast, req) for name, req in declines.items()}
        async with fast() as s:
            logged = (await s.execute(select(func.count()).select_from(ExchangeBid))).scalar()
        return out, logged

    out, logged = run(scenario)
    assert {name: r for name, r in out.items() if r is not None} == {}
    assert logged == 0


@needs_db
def test_an_even_paced_campaign_ahead_of_schedule_sits_out():
    async def scenario(fast, tx):
        cid = await seed(fast, pacing="even")
        async with fast() as s:
            c = await s.get(Campaign, cid)
            allowed = allowed_spend_micros(c)
            await s.execute(update(Campaign).values(
                spent_today_micros=allowed, total_spent_micros=allowed,
                spend_date=datetime.now(timezone.utc).date()))
        return await bid(fast), allowed, await campaign(fast, cid)

    response, allowed, c = run(scenario)
    assert response is None
    assert c.reserved_micros == 0


@needs_db
def test_the_house_ab_ignores_exchange_impressions(control):
    async def scenario(fast, tx):
        await seed(fast)
        b = (await bid(fast)).seatbid[0].bid[0]
        await settle(tx, "settle_win", b.id, 500)
        async with fast() as s:
            return await analytics.ab_comparison(s)

    ab = run(scenario)
    assert sum(arm["impressions"] for arm in ab["arms"]) == 0


@needs_db
def test_the_training_log_holds_settled_bids_with_their_features(control):
    async def scenario(fast, tx):
        await seed(fast)
        won = (await bid(fast, request(id="a"))).seatbid[0].bid[0]
        await bid(fast, request(id="b"))                    # left pending
        await settle(tx, "settle_win", won.id, 500)
        await settle(tx, "record_click", won.id)
        async with fast() as s:
            response = await openrtb_api.training_log(limit=100, db=s)
        body = "".join([chunk async for chunk in response.body_iterator])
        return list(csv.DictReader(io.StringIO(body)))

    rows = run(scenario)
    assert len(rows) == 1
    [row] = rows
    assert row["status"] == "won" and row["clicked"] == "1"
    assert row["country"] == "NG" and row["user_segments"] == "s1,s2"


def test_a_campaign_paces_asap_or_even_and_nothing_else():
    from pydantic import ValidationError
    from app.schemas.schemas import CampaignCreate
    base = dict(advertiser_id="a", name="c", daily_budget_cents=100, total_budget_cents=100,
                max_cpm_cents=50, start_date=datetime(2026, 1, 1))
    assert CampaignCreate(**base).pacing == "asap"
    assert CampaignCreate(**base, pacing="even").pacing == "even"
    with pytest.raises(ValidationError):
        CampaignCreate(**base, pacing="fast")
