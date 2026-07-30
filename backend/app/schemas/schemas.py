from pydantic import BaseModel, Field, HttpUrl
from datetime import datetime
from typing import Optional
from app.models.models import CampaignStatus, AuctionType, DeviceType
import json


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
    auction_type: AuctionType = AuctionType.SECOND_PRICE
    target_countries: list[str] = []
    target_devices: list[str] = []
    target_categories: list[str] = []
    start_date: datetime
    end_date: Optional[datetime] = None

class CampaignOut(BaseModel):
    id: str
    advertiser_id: str
    name: str
    status: CampaignStatus
    daily_budget_cents: int
    total_budget_cents: int
    spent_today_cents: int
    total_spent_cents: int
    max_cpm_cents: int
    auction_type: AuctionType
    remaining_daily_budget_cents: int
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
    highest_bid_cents: int
    num_bidders: int
    auction_type: AuctionType
    latency_ms: float                   # How long the auction took
    strategy: str = "control"           # which A/B arm ran this auction


# ─── Analytics ────────────────────────────────────────────────────────────────

class CampaignStats(BaseModel):
    campaign_id: str
    campaign_name: str
    impressions: int
    clicks: int
    ctr: float                          # Click-through rate %
    total_spend_cents: int
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
    highest_bid_cents: int
    created_at: datetime
    model_config = {"from_attributes": True}
