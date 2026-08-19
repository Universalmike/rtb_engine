"""page_url drives contextual targeting and brand-safety blocking.

The field was previously accepted, validated, and discarded: it appeared in
the request schema and the seeder and was read nowhere. In real RTB the page's
domain (OpenRTB `site.domain`) is core targeting -- advertisers buy contextual
inventory and block domains they will not appear next to.

Semantics follow the targeting fields that already exist: an empty list means
no restriction, so existing campaigns keep bidding on everything.
"""

import json
from datetime import date, datetime, timedelta

import pytest

from app.models.models import (
    AdCreative, AdSlot, AuctionType, Campaign, CampaignStatus, DeviceType,
    Publisher,
)
from app.schemas.schemas import BidRequest
from app.services.auction_engine import AuctionEngine


def campaign(*, allowed=(), blocked=()):
    result = Campaign(
        id="campaign-1", advertiser_id="advertiser-1", name="Test campaign",
        status=CampaignStatus.ACTIVE,
        daily_budget_cents=100, total_budget_cents=1_000,
        spent_today_micros=0, total_spent_micros=0, spend_date=date.today(),
        max_cpm_cents=150, value_per_click_millicents=1_000,
        auction_type=AuctionType.SECOND_PRICE,
        target_countries="[]", target_devices="[]", target_categories="[]",
        target_domains=json.dumps(list(allowed)),
        blocked_domains=json.dumps(list(blocked)),
        start_date=datetime.utcnow() - timedelta(days=1),
    )
    result.creatives = [AdCreative(
        id="creative-1", campaign_id=result.id, name="300x250",
        creative_type="banner", width=300, height=250,
        click_url="https://example.com", asset_url="https://example.com/a.png",
        is_active=True,
    )]
    return result


def slot():
    publisher = Publisher(
        id="publisher-1", name="Publisher", domain="example.com",
        category="tech", is_active=True,
    )
    ad_slot = AdSlot(
        id="slot-1", publisher_id=publisher.id, name="Slot",
        width=300, height=250, floor_price_cents=20,
        device_type=DeviceType.DESKTOP, is_active=True,
    )
    ad_slot.publisher = publisher
    return ad_slot


def engine():
    return AuctionEngine.__new__(AuctionEngine)


def allows(c, url):
    return engine()._passes_targeting(
        c, BidRequest(ad_slot_id="slot-1", page_url=url), slot()
    )


# ── domain extraction ────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://example.com/article/1", "example.com"),
    ("https://www.example.com/", "example.com"),       # www is not meaningful
    ("https://WWW.Example.COM/x", "example.com"),      # host is case-insensitive
    ("https://example.com:8443/x", "example.com"),     # port is not the domain
    ("http://news.example.com/x", "news.example.com"),
    ("not a url", None),
    ("", None),
])
def test_domain_is_extracted_from_the_page_url(url, expected):
    assert AuctionEngine._domain_of(url) == expected


# ── allow list ───────────────────────────────────────────────────────────────

def test_empty_allow_list_bids_on_any_domain():
    """Existing campaigns must keep behaving exactly as before."""
    assert allows(campaign(), "https://anything.example/x") is True


def test_allow_list_admits_a_listed_domain():
    assert allows(campaign(allowed=["example.com"]), "https://example.com/a") is True


def test_allow_list_rejects_an_unlisted_domain():
    assert allows(campaign(allowed=["example.com"]), "https://other.com/a") is False


def test_allow_list_entry_covers_its_subdomains():
    """Buying 'example.com' buys its sections, as a media buyer would expect."""
    c = campaign(allowed=["example.com"])
    assert allows(c, "https://sports.example.com/a") is True


def test_allow_list_does_not_match_a_lookalike_suffix():
    """'example.com' must not match 'notexample.com'."""
    c = campaign(allowed=["example.com"])
    assert allows(c, "https://notexample.com/a") is False


# ── block list ───────────────────────────────────────────────────────────────

def test_blocked_domain_is_rejected():
    assert allows(campaign(blocked=["gambling.com"]), "https://gambling.com/x") is False


def test_blocking_covers_subdomains_too():
    c = campaign(blocked=["gambling.com"])
    assert allows(c, "https://uk.gambling.com/x") is False


def test_a_block_beats_an_allow():
    """Brand safety is a veto, not one vote among several."""
    c = campaign(allowed=["example.com"], blocked=["example.com"])
    assert allows(c, "https://example.com/a") is False


# ── unparseable input ────────────────────────────────────────────────────────

def test_an_unreadable_url_cannot_satisfy_an_allow_list():
    """Targeting that cannot be verified must not be assumed to match."""
    assert allows(campaign(allowed=["example.com"]), "garbage") is False


def test_an_unreadable_url_does_not_trip_a_block_list():
    """A block needs a domain to block; absence of one is not a match."""
    assert allows(campaign(blocked=["gambling.com"]), "garbage") is True
