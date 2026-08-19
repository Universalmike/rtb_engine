from pydantic import BaseModel, Field, model_validator
from datetime import datetime
from typing import Optional
from app.models.models import CampaignStatus, AuctionType, DeviceType


# ─── Advertiser ───────────────────────────────────────────────────────────────

class AdvertiserCreate(BaseModel):
    name: str
    domain: str

class AdvertiserOut(BaseModel):
    id: str
    name: str
    domain: str
    is_active: bool
    created_at: datetime
    model_config = {"from_attributes": True}


# ─── Campaign ─────────────────────────────────────────────────────────────────

class CampaignCreate(BaseModel):
    advertiser_id: str
    name: str
    daily_budget_cents: int = Field(..., gt=0, description="Daily budget in USD cents")
    total_budget_cents: int = Field(..., gt=0)
    max_cpm_cents: int = Field(..., gt=0, description="Max CPM bid in USD cents")
    value_per_click_millicents: Optional[int] = Field(default=None, gt=0)
    auction_type: AuctionType = AuctionType.SECOND_PRICE
    target_countries: list[str] = []
    target_devices: list[str] = []
    target_categories: list[str] = []
    # Page domain (OpenRTB site.domain). Empty target_domains means any
    # domain; blocked_domains vetoes regardless of everything else.
    target_domains: list[str] = []
    blocked_domains: list[str] = []
    start_date: datetime
    end_date: Optional[datetime] = None

    @model_validator(mode="after")
    def validate_budget_and_dates(self):
        if self.total_budget_cents < self.daily_budget_cents:
            raise ValueError("total_budget_cents must be at least daily_budget_cents")
        if self.end_date is not None and self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        return self

class CampaignOut(BaseModel):
    id: str
    advertiser_id: str
    name: str
    status: CampaignStatus
    daily_budget_cents: int
    total_budget_cents: int
    spent_today_cents: float
    total_spent_cents: float
    max_cpm_cents: int
    value_per_click_millicents: int
    auction_type: AuctionType
    remaining_daily_budget_cents: float
    can_bid: bool
    start_date: datetime
    end_date: Optional[datetime]
    created_at: datetime
    model_config = {"from_attributes": True}


# ─── Publisher ────────────────────────────────────────────────────────────────

class PublisherCreate(BaseModel):
    name: str
    domain: str
    category: str = "general"

class AdSlotCreate(BaseModel):
    publisher_id: str
    name: str
    width: int
    height: int
    floor_price_cents: int = 10
    device_type: DeviceType = DeviceType.DESKTOP

class AdSlotOut(BaseModel):
    id: str
    publisher_id: str
    publisher_domain: str = ""
    name: str
    width: int
    height: int
    floor_price_cents: int
    device_type: DeviceType
    model_config = {"from_attributes": True}

class PublisherOut(BaseModel):
    id: str
    name: str
    domain: str
    category: str
    is_active: bool
    created_at: datetime
    model_config = {"from_attributes": True}


# ─── Auction ──────────────────────────────────────────────────────────────────

class BidRequest(BaseModel):
    """
    Sent by a publisher when a user loads a page with an ad slot.
    Contains context about the user/device — no PII in this demo.
    """
    ad_slot_id: str
    country: str = "US"
    device_type: DeviceType = DeviceType.DESKTOP
    page_url: str = "https://example.com"
    user_agent: str = "Mozilla/5.0"

class BidResponse(BaseModel):
    """
    What the auction engine returns after running the auction.
    """
    auction_id: str
    ad_slot_id: str
    had_fill: bool                      # Was there a winner?
    winning_campaign_id: Optional[str]
    winning_creative_id: Optional[str]
    clearing_price_cents: int           # What winner pays
    charged_cost_micros: int = 0         # Exact charge for this impression
    highest_bid_cents: int
    num_bidders: int
    # Why the campaigns that did not bid did not bid, keyed by rule name
    # (country, device, category, domain, blocked_domain, creative, floor).
    # Campaigns filtered in SQL on budget or status never reach the auction
    # and are not counted here.
    excluded: dict[str, int] = {}
    # Content category inferred for this page, and which signal produced it:
    # section (a /sport/ style path), known_domain, keyword, publisher (the
    # URL said nothing, so the site's own category was used), or unknown.
    # Category targeting matches against this, not the publisher's category.
    page_category: Optional[str] = None
    page_category_source: str = "unknown"
    auction_type: AuctionType
    latency_ms: float                   # How long the auction took
    strategy: str = "control"           # which A/B arm ran this auction
    # Per-phase ms breakdown, present only when AUCTION_PROFILE=1 is set.
    timings_ms: Optional[dict[str, float]] = None


# ─── Analytics ────────────────────────────────────────────────────────────────

class CampaignStats(BaseModel):
    campaign_id: str
    campaign_name: str
    impressions: int
    clicks: int
    ctr: float                          # Click-through rate %
    total_spend_cents: float
    avg_cpm_cents: float

class AuctionStats(BaseModel):
    total_auctions: int
    fill_rate: float                    # % auctions that had a winner
    avg_clearing_price_cents: float
    avg_bidders_per_auction: float
    total_impressions: int
    total_clicks: int
    overall_ctr: float

class RecentAuction(BaseModel):
    auction_id: str
    had_fill: bool
    num_bidders: int
    clearing_price_cents: int
    charged_cost_micros: int
    highest_bid_cents: int
    created_at: datetime
    model_config = {"from_attributes": True}
