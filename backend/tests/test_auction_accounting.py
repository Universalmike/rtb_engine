import asyncio
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from app.models.models import (
    AdCreative,
    AdSlot,
    AuctionType,
    Campaign,
    CampaignStatus,
    DeviceType,
    Publisher,
)
from app.schemas.schemas import BidRequest
from app.services.accounting import MICROS_PER_CENT
from app.services.auction_engine import AuctionEngine


def campaign(*, bid=150, auction_type=AuctionType.SECOND_PRICE,
             spent_today=0, total_spent=0):
    result = Campaign(
        id=f"campaign-{bid}",
        advertiser_id="advertiser-1",
        name="Test campaign",
        status=CampaignStatus.ACTIVE,
        daily_budget_cents=100,
        total_budget_cents=1_000,
        spent_today_micros=spent_today,
        total_spent_micros=total_spent,
        spend_date=date.today(),
        max_cpm_cents=bid,
        value_per_click_millicents=1_000,
        auction_type=auction_type,
        target_countries="[]",
        target_devices="[]",
        target_categories="[]",
        start_date=datetime.utcnow() - timedelta(days=1),
    )
    result.creatives = [AdCreative(
        id=f"creative-{bid}", campaign_id=result.id, name="300x250",
        creative_type="banner", width=300, height=250,
        click_url="https://example.com", asset_url="https://example.com/ad.png",
        is_active=True,
    )]
    return result


def slot(*, floor=20, category="tech"):
    publisher = Publisher(
        id="publisher-1", name="Publisher", domain="example.com",
        category=category, is_active=True,
    )
    ad_slot = AdSlot(
        id="slot-1", publisher_id=publisher.id, name="Slot",
        width=300, height=250, floor_price_cents=floor,
        device_type=DeviceType.DESKTOP, is_active=True,
    )
    ad_slot.publisher = publisher
    return ad_slot


def engine_without_connections():
    return AuctionEngine.__new__(AuctionEngine)


def test_control_bid_is_capped_by_exact_remaining_total_budget():
    c = campaign(total_spent=1_000 * MICROS_PER_CENT - 1_509)
    bids = engine_without_connections()._collect_bids(
        [c], slot(), "control", pctr=0.5
    )
    assert bids[0]["bid_cents"] == 150


def test_campaign_is_dropped_when_it_cannot_afford_the_floor_impression():
    c = campaign(total_spent=1_000 * MICROS_PER_CENT - 199)
    bids = engine_without_connections()._collect_bids(
        [c], slot(floor=20), "control", pctr=0.5
    )
    assert bids == []


def test_second_price_charges_second_bid_plus_one():
    high = campaign(bid=150)
    low = campaign(bid=80)
    result = engine_without_connections()._run_price_auction(
        [{"campaign": high, "bid_cents": 150},
         {"campaign": low, "bid_cents": 80}],
        slot(),
    )
    assert result["winner"] is high
    assert result["clearing_price_cents"] == 81


def test_first_price_winner_pays_its_own_bid():
    high = campaign(bid=150, auction_type=AuctionType.FIRST_PRICE)
    low = campaign(bid=80)
    result = engine_without_connections()._run_price_auction(
        [{"campaign": high, "bid_cents": 150},
         {"campaign": low, "bid_cents": 80}],
        slot(),
    )
    assert result["clearing_price_cents"] == 150


def test_category_targeting_is_enforced():
    c = campaign()
    c.target_categories = json.dumps(["finance"])
    request = BidRequest(
        ad_slot_id="slot-1", country="US", device_type="desktop"
    )
    assert engine_without_connections()._passes_targeting(
        c, request, slot(category="tech")
    ) is False


def test_campaign_without_matching_creative_is_not_allowed_to_bid():
    c = campaign()
    c.creatives[0].width = 728
    c.creatives[0].height = 90
    bids = engine_without_connections()._collect_bids(
        [c], slot(), "control", pctr=0.5
    )
    assert bids == []


class FakeResult:
    def __init__(self, row):
        self.row = row

    def one_or_none(self):
        return self.row


class CapturingSession:
    def __init__(self, row):
        self.row = row
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return FakeResult(self.row)


def test_budget_reservation_is_one_conditional_database_update():
    c = campaign()
    row = SimpleNamespace(
        spent_today_micros=810,
        total_spent_micros=1_810,
        status=CampaignStatus.ACTIVE,
    )
    db = CapturingSession(row)
    engine = engine_without_connections()
    engine.db = db

    reserved = asyncio.run(engine._reserve_budget(c, 810))
    compiled = db.statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert reserved is True
    assert sql.startswith("UPDATE campaigns")
    assert "spent_today_micros" in sql and "daily_budget_cents" in sql
    assert "total_spent_micros" in sql and "total_budget_cents" in sql
    assert "CAST(campaigns.daily_budget_cents AS BIGINT)" in sql
    assert "CAST(campaigns.total_budget_cents AS BIGINT)" in sql
    assert "spend_date" in sql
    assert 810 in compiled.params.values()
    assert 10_000 in compiled.params.values()
    assert "RETURNING campaigns.spent_today_micros" in sql


def test_failed_atomic_reservation_does_not_mutate_campaign():
    c = campaign(spent_today=100)
    db = CapturingSession(None)
    engine = engine_without_connections()
    engine.db = db

    assert asyncio.run(engine._reserve_budget(c, 810)) is False
    assert c.spent_today_micros == 100
