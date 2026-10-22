"""A filled auction is one statement, and nothing is announced before it lands.

Measured against a database a few milliseconds away, round trips were the
auction's whole cost: ten of them, plus three more for every pool_pre_ping.
The charge, the auction result, the bid records and the impression now travel
as one statement, which also keeps the campaign's row lock no longer than that
statement takes. These tests pin the shape that makes that true; the
Postgres-backed suite proves it executes.
"""

import asyncio
from types import SimpleNamespace

from sqlalchemy import Select, inspect
from sqlalchemy.dialects import postgresql

import app.services.auction_engine as engine_module
from app.models.models import CampaignStatus
from app.schemas.schemas import BidRequest
from app.services.auction_engine import AuctionEngine, clear_slot_cache
from tests.test_budget_rollover import RecordingSession, campaign, engine_with, slot


def records_for(engine, winner, bids):
    result = {"winner": winner, "highest_bid_cents": 150,
              "clearing_price_cents": 81}
    return engine._auction_records(
        "auction-1", slot(), BidRequest(ad_slot_id="slot-1"), bids, result,
        winner.creatives[0], 810, "control",
    )


def charged(spent=810):
    return SimpleNamespace(spent_today_micros=spent, total_spent_micros=spent,
                           status=CampaignStatus.ACTIVE)


def sql_of(statement):
    return str(statement.compile(dialect=postgresql.dialect()))


# ── one statement ────────────────────────────────────────────────────────────

def test_charge_and_records_are_written_by_a_single_statement():
    session = RecordingSession(returning_row=charged())
    engine = engine_with(session)
    winner = campaign()
    bids = [{"campaign": winner, "bid_cents": 150}]

    assert asyncio.run(engine._reserve_budget(
        winner, 810, records_for(engine, winner, bids))) is True

    assert len(session.statements) == 1
    sql = sql_of(session.statements[0])
    assert sql.startswith("WITH reserved AS")
    assert "UPDATE campaigns" in sql
    for table in ("auction_results", "impressions", "bid_records"):
        assert f"INSERT INTO {table}" in sql


def test_every_insert_reads_from_the_reservation():
    """A charge that matches no row must leave nothing behind.

    Each INSERT selects from the reservation CTE, so when the conditional
    UPDATE refuses (budget gone, campaign paused), every INSERT has zero rows
    to write. Without that, a lost race would record an impression nobody paid
    for.
    """
    engine = engine_with(RecordingSession())
    winner = campaign()
    bids = [{"campaign": winner, "bid_cents": 150}]
    statement = None

    class Capture:
        async def execute(self, s):
            nonlocal statement
            statement = s
            return SimpleNamespace(one_or_none=lambda: None)

    engine.db = Capture()
    reserved = asyncio.run(engine._reserve_budget(
        winner, 810, records_for(engine, winner, bids)))

    assert reserved is False
    sql = sql_of(statement)
    for cte in ("recorded_result", "recorded_impression", "recorded_bids"):
        body = sql.split(f"{cte} AS")[1].split("\n),")[0]
        assert "reserved" in body, f"{cte} does not depend on the charge"


def test_bid_records_sql_does_not_change_with_the_number_of_bidders():
    """One statement text for any bid count, so it is compiled and prepared once.

    A VALUES list made each bidder count a distinct statement and disabled
    SQLAlchemy's compiled cache for all of them.
    """
    engine = engine_with(RecordingSession())

    def sql_for(n):
        session = RecordingSession(returning_row=charged())
        engine.db = session
        bidders = [campaign() for _ in range(n)]
        bids = [{"campaign": c, "bid_cents": 150 - i} for i, c in enumerate(bidders)]
        asyncio.run(engine._reserve_budget(
            bidders[0], 810, records_for(engine, bidders[0], bids)))
        return sql_of(session.statements[0])

    assert sql_for(1) == sql_for(4)
    assert "unnest" in sql_for(2)


def test_a_successful_charge_does_not_queue_a_second_update():
    """The spend the database returned must not be flushed straight back."""
    c = campaign()
    session = RecordingSession(returning_row=charged(spent=810))

    assert asyncio.run(engine_with(session)._reserve_budget(c, 810)) is True

    assert c.spent_today_micros == 810
    attrs = inspect(c).attrs
    for name in ("spent_today_micros", "total_spent_micros", "status"):
        assert not attrs[name].history.has_changes(), (
            f"{name} would be flushed back as a second UPDATE")


# ── events wait for the write ────────────────────────────────────────────────

class FakePipeline:
    def __init__(self, redis):
        self.redis = redis

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def xadd(self, stream, event):
        self.redis.queued.append(event)

    async def execute(self):
        if self.redis.fail:
            raise ConnectionError("redis down")
        self.redis.round_trips += 1
        self.redis.sent.extend(self.redis.queued)


class FakeRedis:
    def __init__(self, fail=False):
        self.fail, self.queued, self.sent, self.round_trips = fail, [], [], 0

    def pipeline(self, transaction=True):
        return FakePipeline(self)


def engine_with_redis(redis):
    engine = engine_with(RecordingSession())
    engine.redis = redis
    return engine


def queue_two(engine):
    for auction_id in ("a-1", "a-2"):
        engine._queue_impression_event(
            auction_id, campaign(), slot(), 81, 810,
            BidRequest(ad_slot_id="slot-1"))


def test_events_are_held_until_published():
    redis = FakeRedis()
    engine = engine_with_redis(redis)

    async def scenario():
        queue_two(engine)
        await asyncio.sleep(0)
        assert redis.sent == [], "event left before its write was durable"
        engine.publish_events()
        await asyncio.gather(*engine_module._background_tasks)

    asyncio.run(scenario())
    assert [e["auction_id"] for e in redis.sent] == ["a-1", "a-2"]
    assert redis.round_trips == 1, "a batch must be one pipeline, not N calls"


def test_a_redis_outage_costs_events_not_auctions():
    engine = engine_with_redis(FakeRedis(fail=True))

    async def scenario():
        queue_two(engine)
        engine.publish_events()
        await asyncio.gather(*engine_module._background_tasks)

    asyncio.run(scenario())   # must not raise
    assert engine.pending_events == []


# ── slot cache ───────────────────────────────────────────────────────────────

class ExpungingSession(RecordingSession):
    def expunge(self, obj):
        pass


def test_a_slot_is_fetched_once_then_served_from_cache():
    clear_slot_cache()
    session = ExpungingSession(rows=[slot()])
    engine = engine_with(session)
    try:
        first = asyncio.run(engine._get_ad_slot("slot-1"))
        second = asyncio.run(engine._get_ad_slot("slot-1"))
    finally:
        clear_slot_cache()

    assert first is second
    assert len(session.statements) == 1


def test_an_unknown_slot_is_not_cached_as_missing():
    """A slot created a moment after a miss must be found."""
    clear_slot_cache()
    session = ExpungingSession(rows=[])
    engine = engine_with(session)

    assert asyncio.run(engine._get_ad_slot("slot-1")) is None
    session.rows = [slot()]
    assert asyncio.run(engine._get_ad_slot("slot-1")) is not None
    clear_slot_cache()


# ── eligibility loads only what can serve ────────────────────────────────────

def test_eligibility_loads_only_creatives_that_fit_the_slot():
    session = RecordingSession(rows=[])
    asyncio.run(engine_with(session)._query_solvent_campaigns(slot()))

    statement = session.statements[0]
    assert isinstance(statement, Select)
    join = sql_of(statement).split("LEFT OUTER JOIN ad_creatives")[1]
    assert "width" in join and "height" in join and "is_active" in join
