"""
RTB Data Model — mirrors real production AdTech architecture.

Hierarchy:
  Advertiser → Campaign → AdCreative
  Publisher  → AdSlot
  AuctionResult links a winning bid to a slot
  Impression / Click are event records
"""

import uuid
from typing import Optional
from datetime import date, datetime, timezone
from sqlalchemy import (
    String, Boolean, DateTime, ForeignKey,
    Integer, BigInteger, Date, Float, Text, Enum as SAEnum, Index
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
import enum

from app.core.database import Base
from app.services.accounting import MICROS_PER_CENT, micros_to_cents


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

    # Budget limits use whole cents; spend uses integer microdollars because a
    # single CPM-priced impression normally costs a fraction of one cent.
    daily_budget_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    total_budget_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    spent_today_micros: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_spent_micros: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    spend_date: Mapped[date] = mapped_column(
        Date, default=lambda: datetime.utcnow().date(), nullable=False
    )
    # Held by open budget leases (see app/services/leasing.py): budget set
    # aside for exchange bids, counted against both budgets everywhere, so
    # bids in flight cannot add up to more than the campaign has left.
    reserved_micros: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False)
    # asap spends as opportunities come; even holds spend to the share of the
    # day that has passed (see app/services/pacing.py).
    pacing: Mapped[str] = mapped_column(
        String(10), default="asap", server_default="asap", nullable=False)

    # Bidding
    max_cpm_cents: Mapped[int] = mapped_column(Integer, nullable=False)  # Max bid per 1000 impressions
    # Value of one click to the advertiser, in millicents (1/1000 cent). Stored
    # finer than cents because Avazu's ~16% CTR makes the fair per-click value
    # sub-one-cent — integer cents would collapse every campaign to the same 1.
    # EV bid (CPM cents) = pCTR * value_per_click_millicents.
    value_per_click_millicents: Mapped[int] = mapped_column(Integer, default=0)
    auction_type: Mapped[AuctionType] = mapped_column(SAEnum(AuctionType), default=AuctionType.SECOND_PRICE)

    # Targeting
    target_countries: Mapped[str] = mapped_column(Text, default="[]")    # JSON array e.g. ["NG","US","GB"]
    target_devices: Mapped[str] = mapped_column(Text, default="[]")      # JSON array of DeviceType values
    target_categories: Mapped[str] = mapped_column(Text, default="[]")   # IAB content categories
    # Domain of the page the ad would appear on (OpenRTB `site.domain`).
    # target_domains is contextual buying: empty means any domain.
    # blocked_domains is brand safety: a veto, checked before anything else.
    target_domains: Mapped[str] = mapped_column(
        Text, default="[]", server_default="[]")
    blocked_domains: Mapped[str] = mapped_column(
        Text, default="[]", server_default="[]")

    start_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    advertiser: Mapped["Advertiser"] = relationship("Advertiser", back_populates="campaigns")
    creatives: Mapped[list["AdCreative"]] = relationship("AdCreative", back_populates="campaign")
    bids: Mapped[list["BidRecord"]] = relationship("BidRecord", back_populates="campaign")

    __table_args__ = (
        Index("ix_campaigns_advertiser_status", "advertiser_id", "status"),
        # Every auction opens by selecting solvent campaigns on status, then
        # comparing spend_date against today. The advertiser_status index
        # cannot serve that: advertiser_id leads it and the auction never
        # filters on one.
        Index("ix_campaigns_status_spend_date", "status", "spend_date"),
    )

    @property
    def daily_budget_micros(self) -> int:
        return self.daily_budget_cents * MICROS_PER_CENT

    @property
    def total_budget_micros(self) -> int:
        return self.total_budget_cents * MICROS_PER_CENT

    @property
    def spent_today_cents(self) -> float:
        """Compatibility/reporting view; the database source is integer micros."""
        return micros_to_cents(self.spent_today_micros)

    @property
    def total_spent_cents(self) -> float:
        return micros_to_cents(self.total_spent_micros)

    @property
    def effective_spent_today_micros(self) -> int:
        """Today's spend, reading a stale spend_date as nothing spent yet.

        Spend carried from an earlier day has effectively rolled over already;
        the row is only rewritten when the campaign is next charged, so a
        stale spend_date must read as zero spent rather than yesterday's
        total. See AuctionEngine._spent_today_expression for the SQL twin.
        """
        return (
            self.spent_today_micros
            if self.spend_date == datetime.now(timezone.utc).date()
            else 0
        )

    @property
    def remaining_daily_budget_micros(self) -> int:
        """Today's budget left after spend and in-flight reservations."""
        return max(0, self.daily_budget_micros - self.effective_spent_today_micros
                   - (self.reserved_micros or 0))

    @property
    def remaining_total_budget_micros(self) -> int:
        return max(0, self.total_budget_micros - self.total_spent_micros
                   - (self.reserved_micros or 0))

    @property
    def remaining_daily_budget_cents(self) -> float:
        return micros_to_cents(self.remaining_daily_budget_micros)

    @property
    def can_bid(self) -> bool:
        return (
            self.status == CampaignStatus.ACTIVE
            and min(
                self.remaining_daily_budget_micros,
                self.remaining_total_budget_micros,
            ) > 0
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
    banner_pos: Mapped[int] = mapped_column(Integer, default=0)  # Avazu-convention slot position
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    publisher: Mapped["Publisher"] = relationship("Publisher", back_populates="ad_slots")
    auction_results: Mapped[list["AuctionResult"]] = relationship("AuctionResult", back_populates="ad_slot")

    @property
    def publisher_domain(self) -> str:
        """Domain of the publisher that owns this slot.

        A bid request's page_url should be a page *on this domain* -- the slot
        is inventory on that publisher's site. Exposing it lets a caller build
        a coherent request instead of guessing a URL that matches nothing.
        Requires the publisher relationship to be loaded.
        """
        return self.publisher.domain if self.publisher else ""


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
    The clearing CPM quote plus the exact per-impression amount charged.
    """
    __tablename__ = "auction_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    auction_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    # Null for an impression bought on an exchange: the slot is the publisher's.
    ad_slot_id: Mapped[Optional[str]] = mapped_column(ForeignKey("ad_slots.id"), nullable=True)
    # house: the engine's own auction over its slots; openrtb: an impression
    # won on an exchange. Their clicks differ in kind (simulated vs real), so
    # the house A/B reads only its own.
    source: Mapped[str] = mapped_column(
        String(10), default="house", server_default="house", nullable=False)
    winning_campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), nullable=True)
    winning_creative_id: Mapped[str] = mapped_column(ForeignKey("ad_creatives.id"), nullable=True)
    highest_bid_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    clearing_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    charged_cost_micros: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    num_bidders: Mapped[int] = mapped_column(Integer, default=0)
    strategy: Mapped[str] = mapped_column(String(20), default="control")  # control | treatment
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
    ad_slot_id: Mapped[Optional[str]] = mapped_column(ForeignKey("ad_slots.id"), nullable=True)
    source: Mapped[str] = mapped_column(
        String(10), default="house", server_default="house", nullable=False)
    clearing_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    charged_cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
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


# ─── Exchange bidding (OpenRTB) ───────────────────────────────────────────────

class BudgetLease(Base):
    """
    A slice of a campaign's budget set aside for one process's exchange bids.

    Taking the lease is the one write to the campaign's row; the process then
    admits bids against it in memory. Every bid's maximum cost comes out of a
    lease the database already holds, so no number of processes bidding at
    once can overspend. A closed lease returns what its wins did not charge.
    """
    __tablename__ = "budget_leases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    owner: Mapped[str] = mapped_column(String(100), nullable=False)
    amount_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    charged_micros: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # open -> closed. Closing gives back amount - charged.
    status: Mapped[str] = mapped_column(String(10), default="open", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    # No new bid is placed on the lease after this; it closes once its last
    # bids are past their notice TTL as well.
    usable_until: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    released_micros: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        Index("ix_budget_leases_status_usable", "status", "usable_until"),
    )


class ExchangeBid(Base):
    """
    Every bid sent to an exchange, and what became of it.

    The row is the bid's notice target and doubles as the feature log: the
    model's inputs, its prediction and the outcome, which is the training
    data the next model learns from.
    """
    __tablename__ = "exchange_bids"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    imp_id: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    creative_id: Mapped[str] = mapped_column(ForeignKey("ad_creatives.id"), nullable=False)
    # The lease whose budget covers this bid's maximum cost.
    lease_id: Mapped[str] = mapped_column(ForeignKey("budget_leases.id"), nullable=False)
    strategy: Mapped[str] = mapped_column(String(20), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    pctr: Mapped[float] = mapped_column(Float, nullable=False)
    features: Mapped[str] = mapped_column(Text, nullable=False)   # JSON
    bid_cents: Mapped[int] = mapped_column(Integer, nullable=False)  # CPM
    max_cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # pending -> won | lost | expired. Money moves on the lease, not here.
    status: Mapped[str] = mapped_column(String(10), default="pending", nullable=False)
    charged_micros: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    impression_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    loss_reason: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    clicked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        # The sweeper looks for pending bids older than their TTL.
        Index("ix_exchange_bids_status_created", "status", "created_at"),
    )
