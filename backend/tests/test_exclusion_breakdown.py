"""The auction reports why campaigns did not bid, not just how many did.

`num_bidders: 2` tells a visitor nothing about the other fifteen. Without a
reason, changing the page URL and watching nothing happen is indistinguishable
from the field being ignored -- which it genuinely was until recently. The
breakdown makes every input legible, including one that matches nothing.
"""

import asyncio
import json
from datetime import date, datetime, timedelta

from app.models.models import (
    AdCreative, AuctionType, Campaign, CampaignStatus,
)
from app.schemas.schemas import BidRequest
from app.services.accounting import MICROS_PER_CENT
from app.services.auction_engine import AuctionEngine
from tests.test_budget_rollover import RecordingSession, engine_with
from tests.test_domain_targeting import slot


def campaign(*, countries=(), devices=(), categories=(),
             allowed=(), blocked=(), total_spent=0, creative_size=(300, 250)):
    result = Campaign(
        id="campaign-1", advertiser_id="advertiser-1", name="Test campaign",
        status=CampaignStatus.ACTIVE,
        daily_budget_cents=100_000, total_budget_cents=1_000,
        spent_today_micros=0, total_spent_micros=total_spent,
        spend_date=date.today(),
        max_cpm_cents=150, value_per_click_millicents=1_000,
        auction_type=AuctionType.SECOND_PRICE,
        target_countries=json.dumps(list(countries)),
        target_devices=json.dumps(list(devices)),
        target_categories=json.dumps(list(categories)),
        target_domains=json.dumps(list(allowed)),
        blocked_domains=json.dumps(list(blocked)),
        start_date=datetime.utcnow() - timedelta(days=1),
    )
    width, height = creative_size
    result.creatives = [AdCreative(
        id="creative-1", campaign_id=result.id, name="creative",
        creative_type="banner", width=width, height=height,
        click_url="https://example.com", asset_url="https://example.com/a.png",
        is_active=True,
    )]
    return result


def request(**kw):
    kw.setdefault("ad_slot_id", "slot-1")
    kw.setdefault("country", "NG")
    kw.setdefault("page_url", "https://example.com/a")
    return BidRequest(**kw)


def reason_for(c, req=None):
    return AuctionEngine.__new__(AuctionEngine)._targeting_rejection(
        c, req or request(), slot()
    )


# ── each rule names itself ───────────────────────────────────────────────────

def test_a_passing_campaign_has_no_rejection_reason():
    assert reason_for(campaign()) is None


def test_country_mismatch_is_named():
    assert reason_for(campaign(countries=["US"])) == "country"


def test_device_mismatch_is_named():
    assert reason_for(campaign(devices=["mobile"])) == "device"


def test_category_mismatch_is_named():
    assert reason_for(campaign(categories=["finance"])) == "category"


def test_domain_not_on_the_allow_list_is_named():
    assert reason_for(campaign(allowed=["other.com"])) == "domain"


def test_a_blocked_domain_is_reported_separately_from_a_missed_allow_list():
    """They are different decisions and a visitor should see which one fired."""
    assert reason_for(campaign(blocked=["example.com"])) == "blocked_domain"
    assert reason_for(
        campaign(allowed=["example.com"], blocked=["example.com"])
    ) == "blocked_domain"


# ── the counts reach the caller ──────────────────────────────────────────────

def test_eligibility_returns_counts_alongside_the_survivors():
    session = RecordingSession(rows=[
        campaign(),                    # bids
        campaign(countries=["US"]),    # excluded: country
        campaign(allowed=["other.com"]),  # excluded: domain
    ])
    eligible, excluded = asyncio.run(
        engine_with(session)._get_eligible_campaigns(request(), slot())
    )
    assert len(eligible) == 1
    assert excluded == {"country": 1, "domain": 1}


def test_a_campaign_with_no_creative_for_the_slot_is_counted():
    excluded = {}
    bids = AuctionEngine.__new__(AuctionEngine)._collect_bids(
        [campaign(creative_size=(728, 90))], slot(), "control", 0.5, excluded
    )
    assert bids == []
    assert excluded == {"creative": 1}


def test_a_campaign_that_cannot_clear_the_floor_is_counted():
    excluded = {}
    broke = campaign(total_spent=1_000 * MICROS_PER_CENT - 199)
    bids = AuctionEngine.__new__(AuctionEngine)._collect_bids(
        [broke], slot(), "control", 0.5, excluded
    )
    assert bids == []
    assert excluded == {"floor": 1}


# ── it reaches the response, especially when nothing filled ──────────────────

def test_a_no_fill_response_still_explains_itself():
    """This is the case a visitor most needs explained."""
    session = RecordingSession(rows=[])
    response = asyncio.run(
        engine_with(session).run_auction(request(ad_slot_id="missing"))
    )
    assert response.had_fill is False
    assert response.excluded == {}
