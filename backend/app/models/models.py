"""
RTB Data Model — mirrors real production AdTech architecture.

Hierarchy:
  Advertiser → Campaign → AdCreative
  Publisher  → AdSlot
  AuctionResult links a winning bid to a slot
  Impression / Click are event records
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    String, Numeric, Boolean, DateTime, ForeignKey,
    Integer, Text, Enum as SAEnum, Index
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
import enum

from app.core.database import Base


def gen_uuid():
    return str(uuid.uuid4())


# ─── Enums ────────────────────────────────────────────────────────────────────

class CampaignStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    EXHAUSTED = "exhausted"   # Budget fully spent
    ENDED = "ended"


class AuctionType(str, enum.Enum):
    SECOND_PRICE = "second_price"   # Winner pays 2nd highest bid + $0.01
    FIRST_PRICE = "first_price"     # Winner pays their own bid


class DeviceType(str, enum.Enum):
    DESKTOP = "desktop"
    MOBILE = "mobile"
    TABLET = "tablet"
    CTV = "ctv"                     # Connected TV (Roku, Fire TV, etc.)


# ─── Advertiser ───────────────────────────────────────────────────────────────

class Advertiser(Base):
    """
    A brand or agency buying ad inventory.
    In real RTB these are DSP (Demand Side Platform) clients.
    """
    __tablename__ = "advertisers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    campaigns: Mapped[list["Campaign"]] = relationship("Campaign", back_populates="advertiser")


# ─── Campaign ─────────────────────────────────────────────────────────────────

class Campaign(Base):
    """
    A campaign is a budget envelope with targeting rules.
    Budget pacing = how fast you spend your daily budget.
    """
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    advertiser_id: Mapped[str] = mapped_column(ForeignKey("advertisers.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[CampaignStatus] = mapped_column(SAEnum(CampaignStatus), default=CampaignStatus.ACTIVE)

    # Budget — stored in USD cents to avoid floating point errors
    daily_budget_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    total_budget_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    spent_today_cents: Mapped[int] = mapped_column(Integer, default=0)
    total_spent_cents: Mapped[int] = mapped_column(Integer, default=0)

    # Bidding
    max_cpm_cents: Mapped[int] = mapped_column(Integer, nullable=False)  # Max bid per 1000 impressions
    auction_type: Mapped[AuctionType] = mapped_column(SAEnum(AuctionType), default=AuctionType.SECOND_PRICE)

    # Targeting
    target_countries: Mapped[str] = mapped_column(Text, default="[]")    # JSON array e.g. ["NG","US","GB"]
    target_devices: Mapped[str] = mapped_column(Text, default="[]")      # JSON array of DeviceType values
    target_categories: Mapped[str] = mapped_column(Text, default="[]")   # IAB content categories

    start_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    advertiser: Mapped["Advertiser"] = relationship("Advertiser", back_populates="campaigns")
    creatives: Mapped[list["AdCreative"]] = relationship("AdCreative", back_populates="campaign")
    bids: Mapped[list["BidRecord"]] = relationship("BidRecord", back_populates="campaign")

    __table_args__ = (
        Index("ix_campaigns_advertiser_status", "advertiser_id", "status"),
    )

    @property
    def remaining_daily_budget_cents(self) -> int:
        return max(0, self.daily_budget_cents - self.spent_today_cents)

    @property
    def can_bid(self) -> bool:
        return (
            self.status == CampaignStatus.ACTIVE
            and self.remaining_daily_budget_cents > 0
        )


# ─── AdCreative ───────────────────────────────────────────────────────────────

class AdCreative(Base):
    """
    The actual ad unit — banner, video, native.
    A campaign can have multiple creatives (A/B testing).
    """
    __tablename__ = "ad_creatives"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    creative_type: Mapped[str] = mapped_column(String(50), default="banner")  # banner | video | native
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    click_url: Mapped[str] = mapped_column(Text, nullable=False)
    asset_url: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    campaign: Mapped["Campaign"] = relationship("Campaign", back_populates="creatives")


# ─── Publisher & AdSlot ───────────────────────────────────────────────────────

class Publisher(Base):
    """
    A website or app selling ad inventory (the Supply Side).
    In real RTB these connect via an SSP (Supply Side Platform).
    """
    __tablename__ = "publishers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(100), default="general")  # IAB category
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    ad_slots: Mapped[list["AdSlot"]] = relationship("AdSlot", back_populates="publisher")


class AdSlot(Base):
    """
    A specific placement on a publisher's page.
    E.g. 'homepage leaderboard 728x90' or 'article sidebar 300x250'.
    """
    __tablename__ = "ad_slots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    publisher_id: Mapped[str] = mapped_column(ForeignKey("publishers.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    floor_price_cents: Mapped[int] = mapped_column(Integer, default=10)  # Min acceptable bid in cents CPM
    device_type: Mapped[DeviceType] = mapped_column(SAEnum(DeviceType), default=DeviceType.DESKTOP)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    publisher: Mapped["Publisher"] = relationship("Publisher", back_populates="ad_slots")
    auction_results: Mapped[list["AuctionResult"]] = relationship("AuctionResult", back_populates="ad_slot")


# ─── BidRecord ────────────────────────────────────────────────────────────────

class BidRecord(Base):
    """
    Every bid submitted in every auction, win or lose.
    This is your audit trail — critical for billing and dispute resolution.
    """
    __tablename__ = "bid_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    auction_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    bid_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    is_winner: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    campaign: Mapped["Campaign"] = relationship("Campaign", back_populates="bids")

    __table_args__ = (
        Index("ix_bid_records_auction_winner", "auction_id", "is_winner"),
    )


# ─── AuctionResult ────────────────────────────────────────────────────────────

class AuctionResult(Base):
    """
    The outcome of a completed auction.
    Clearing price = what the winner actually pays (second-price logic).
    """
    __tablename__ = "auction_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    auction_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    ad_slot_id: Mapped[str] = mapped_column(ForeignKey("ad_slots.id"), nullable=False)
    winning_campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), nullable=True)
    winning_creative_id: Mapped[str] = mapped_column(ForeignKey("ad_creatives.id"), nullable=True)
    highest_bid_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    clearing_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)  # What winner pays
    num_bidders: Mapped[int] = mapped_column(Integer, default=0)
    had_fill: Mapped[bool] = mapped_column(Boolean, default=False)  # False = no bids above floor
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    ad_slot: Mapped["AdSlot"] = relationship("AdSlot", back_populates="auction_results")


# ─── Impression / Click ────────────────────────────────────────────────────────

class Impression(Base):
    """
    Fired when a winning ad is actually rendered in the browser.
    This is what gets billed — not the bid, the impression.
    """
    __tablename__ = "impressions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    auction_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    ad_slot_id: Mapped[str] = mapped_column(ForeignKey("ad_slots.id"), nullable=False)
    clearing_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=True)
    device_type: Mapped[str] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_impressions_campaign_date", "campaign_id", "created_at"),
    )


class Click(Base):
    """
    Fired when a user clicks the ad.
    CTR (Click-Through Rate) = clicks / impressions.
    """
    __tablename__ = "clicks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    impression_id: Mapped[str] = mapped_column(ForeignKey("impressions.id"), nullable=False)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
