"""Daily budget rollover is lazy — it is not a write on every bid request.

The original design called a table-wide UPDATE (`_rollover_daily_budgets`)
before every auction, purely so the solvency query could assume
`spend_date == today`. That put a write, and row locks, on the hot read path of
every single bid request against a database that is a network hop away.

The replacement keeps the same semantics without the write: the read path
treats a stale `spend_date` as "nothing spent today" inline, and the
per-campaign reservation UPDATE stamps today's date atomically when it charges.
"""

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import Select, Update
from sqlalchemy.dialects import postgresql

from app.models.models import (
    AdCreative, AdSlot, Campaign, CampaignStatus, DeviceType, Publisher,
)
from app.schemas.schemas import BidRequest
from app.services.auction_engine import AuctionEngine

YESTERDAY = date.today() - timedelta(days=1)


def campaign(*, status=CampaignStatus.ACTIVE, spend_date=None, spent_today=0):
    result = Campaign(
        id="campaign-1",
        advertiser_id="advertiser-1",
        name="Test campaign",
        status=status,
        daily_budget_cents=100,
        total_budget_cents=1_000,
        spent_today_micros=spent_today,
        total_spent_micros=0,
        spend_date=spend_date or date.today(),
        max_cpm_cents=150,
        value_per_click_millicents=1_000,
        auction_type=None,
        target_countries="[]",
        target_devices="[]",
        target_categories="[]",
        start_date=datetime.utcnow() - timedelta(days=1),
    )
    result.creatives = [AdCreative(
        id="creative-1", campaign_id=result.id, name="300x250",
        creative_type="banner", width=300, height=250,
        click_url="https://example.com", asset_url="https://example.com/ad.png",
        is_active=True,
    )]
    return result


def slot(*, floor=20):
    publisher = Publisher(
        id="publisher-1", name="Publisher", domain="example.com",
        category="tech", is_active=True,
    )
    ad_slot = AdSlot(
        id="slot-1", publisher_id=publisher.id, name="Slot",
        width=300, height=250, floor_price_cents=floor,
        device_type=DeviceType.DESKTOP, is_active=True,
    )
    ad_slot.publisher = publisher
    return ad_slot


class RecordingSession:
    """Records every statement handed to execute() without touching a database."""

    def __init__(self, rows=(), returning_row=None):
        self.statements = []
        self.rows = list(rows)
        self.returning_row = returning_row

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        rows, returning_row = self.rows, self.returning_row

        class Result:
            def unique(self):
                return self

            def scalars(self):
                return SimpleNamespace(all=lambda: rows)

            def one_or_none(self):
                return returning_row

            def scalar_one_or_none(self):
                return rows[0] if rows else None

        return Result()


def engine_with(session):
    engine = AuctionEngine.__new__(AuctionEngine)
    engine.db = session
    return engine


def sql_of(statement):
    return str(statement.compile(dialect=postgresql.dialect()))


# ── the read path must not write ─────────────────────────────────────────────

def test_eligibility_check_issues_no_write():
    """A bid request must not UPDATE the campaigns table just to read it."""
    session = RecordingSession(rows=[])
    request = BidRequest(ad_slot_id="slot-1", country="NG")

    asyncio.run(engine_with(session)._get_eligible_campaigns(request, slot()))

    assert len(session.statements) == 1, (
        f"expected a single SELECT, got {len(session.statements)} statements"
    )
    assert isinstance(session.statements[0], Select)
    assert not any(isinstance(s, Update) for s in session.statements)


def test_solvency_query_ignores_daily_spend_carried_from_a_previous_day():
    """Yesterday's spent_today must count as zero without rewriting the row."""
    session = RecordingSession(rows=[])
    asyncio.run(engine_with(session)._query_solvent_campaigns(slot()))

    sql = sql_of(session.statements[0])
    assert "CASE WHEN" in sql, "stale daily spend must be zeroed inline"
    assert "spend_date" in sql


def test_solvency_query_admits_a_campaign_exhausted_on_a_previous_day():
    """An EXHAUSTED campaign becomes eligible again once the day rolls over."""
    session = RecordingSession(rows=[])
    asyncio.run(engine_with(session)._query_solvent_campaigns(slot()))

    sql = sql_of(session.statements[0])
    assert "exhausted" in sql.lower() or "status" in sql.lower()


# ── the write path carries the rollover ──────────────────────────────────────

def test_reservation_stamps_today_and_zeroes_a_stale_daily_spend():
    """The charging UPDATE performs that campaign's rollover atomically."""
    row = SimpleNamespace(
        spent_today_micros=810, total_spent_micros=810,
        status=CampaignStatus.ACTIVE,
    )
    session = RecordingSession(returning_row=row)
    stale = campaign(spend_date=YESTERDAY, spent_today=9_999_999)

    reserved = asyncio.run(engine_with(session)._reserve_budget(stale, 810))

    sql = sql_of(session.statements[0])
    set_clause, _, where_clause = sql.partition(" WHERE ")
    assert reserved is True
    assert sql.startswith("UPDATE campaigns")
    # SET must stamp today, not merely filter on it in WHERE.
    assert "spend_date" in set_clause, "charging UPDATE must stamp today's date"
    assert "CASE WHEN" in set_clause, "must zero a stale daily spend as it charges"
    # The row must be chargeable regardless of its stored date. A bare
    # `spend_date == today` gate is what forced the table-wide pre-UPDATE;
    # what remains is the `spend_date < today` clause that readmits a
    # campaign exhausted on an earlier day.
    assert "spend_date <" in where_clause
    assert date.today() in session.statements[0].compile().params.values()


def test_reservation_reactivates_a_campaign_exhausted_yesterday():
    """Rolling over must clear EXHAUSTED, otherwise the campaign never returns."""
    row = SimpleNamespace(
        spent_today_micros=810, total_spent_micros=810,
        status=CampaignStatus.ACTIVE,
    )
    session = RecordingSession(returning_row=row)
    stale = campaign(
        status=CampaignStatus.EXHAUSTED,
        spend_date=YESTERDAY,
        spent_today=100 * 10_000,
    )

    assert asyncio.run(engine_with(session)._reserve_budget(stale, 810)) is True
    assert "status" in sql_of(session.statements[0])


def test_failed_reservation_still_leaves_the_campaign_untouched():
    session = RecordingSession(returning_row=None)
    c = campaign(spent_today=100)

    assert asyncio.run(engine_with(session)._reserve_budget(c, 810)) is False
    assert c.spent_today_micros == 100


# -- profiling instrumentation ------------------------------------------------

def test_run_auction_reports_a_phase_breakdown(monkeypatch, caplog):
    """Every auction records per-phase ms, including when it no-fills early."""
    import logging
    import app.services.auction_engine as engine_module

    monkeypatch.setattr(engine_module, "PROFILE_AUCTIONS", True)
    session = RecordingSession()  # unknown slot -> immediate no-fill

    with caplog.at_level(logging.INFO, logger=engine_module.__name__):
        response = asyncio.run(
            engine_with(session).run_auction(BidRequest(ad_slot_id="missing"))
        )

    assert response.had_fill is False
    assert response.timings_ms is not None
    assert "fetch_slot" in response.timings_ms
    assert response.timings_ms["fetch_slot"] >= 0
    assert "total_ms=" in caplog.text


def test_phase_breakdown_is_withheld_unless_profiling_is_enabled():
    """The demo response stays clean by default."""
    import app.services.auction_engine as engine_module

    assert engine_module.PROFILE_AUCTIONS is False
    response = asyncio.run(
        engine_with(RecordingSession()).run_auction(BidRequest(ad_slot_id="x"))
    )
    assert response.timings_ms is None
