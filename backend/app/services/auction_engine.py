"""
RTB Auction Engine

How a real auction works (simplified):
1. Publisher page loads → sends bid request with ad slot context
2. Engine fetches all eligible campaigns (active, budget available, targeting match)
3. Each campaign submits a bid (their max CPM)
4. Second-price auction: winner pays 2nd highest bid + 1 cent (Vickrey auction)
5. Result is stored, impression event is fired to Redis Stream
6. Budget is decremented on the winning campaign

Second-price auction theory: bidders are incentivized to bid their true value
because they never pay more than the second-highest bid. This is the same
mechanism used by Google Ad Exchange, AppNexus, and most major DSPs.
"""

import uuid
import time
import json
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload

from app.models.models import (
    Campaign, CampaignStatus, AdSlot, AdCreative,
    BidRecord, AuctionResult, Impression, DeviceType
)
from app.schemas.schemas import BidRequest, BidResponse, AuctionType
from app.core.redis_client import get_redis, IMPRESSION_STREAM, BID_STREAM
from app.core.config import get_settings

settings = get_settings()


class AuctionEngine:

    def __init__(self, db: AsyncSession):
        self.db = db
        self.redis = get_redis()

    async def run_auction(self, bid_request: BidRequest) -> BidResponse:
        """
        Main auction flow. Returns a BidResponse in under 100ms (our SLA).
        """
        start_time = time.monotonic()
        auction_id = str(uuid.uuid4())

        # 1. Fetch the ad slot
        ad_slot = await self._get_ad_slot(bid_request.ad_slot_id)
        if not ad_slot:
            return self._no_fill_response(auction_id, bid_request, start_time)

        # 2. Get eligible campaigns
        eligible_campaigns = await self._get_eligible_campaigns(bid_request, ad_slot)

        if not eligible_campaigns:
            await self._save_no_fill_result(auction_id, ad_slot, start_time)
            return self._no_fill_response(auction_id, bid_request, start_time)

        # 3. Collect bids — each campaign bids its max CPM
        bids = self._collect_bids(eligible_campaigns, ad_slot)

        if not bids:
            await self._save_no_fill_result(auction_id, ad_slot, start_time)
            return self._no_fill_response(auction_id, bid_request, start_time)

        # 4. Run the auction
        result = self._run_second_price_auction(bids, ad_slot)
        winning_campaign = result["winner"]
        clearing_price = result["clearing_price_cents"]

        # 5. Pick a creative from the winning campaign
        creative = await self._pick_creative(winning_campaign)

        # 6. Save bid records (all bids, win/lose)
        await self._save_bid_records(auction_id, bids)

        # 7. Save auction result
        await self._save_auction_result(
            auction_id, ad_slot, winning_campaign, creative,
            result["highest_bid_cents"], clearing_price, len(bids)
        )

        # 8. Deduct budget from winning campaign
        await self._deduct_budget(winning_campaign, clearing_price)

        # 9. Fire impression event to Redis Stream
        await self._emit_impression_event(
            auction_id, winning_campaign, ad_slot, clearing_price, bid_request
        )

        latency = (time.monotonic() - start_time) * 1000

        return BidResponse(
            auction_id=auction_id,
            ad_slot_id=bid_request.ad_slot_id,
            had_fill=True,
            winning_campaign_id=winning_campaign.id,
            winning_creative_id=creative.id if creative else None,
            clearing_price_cents=clearing_price,
            highest_bid_cents=result["highest_bid_cents"],
            num_bidders=len(bids),
            auction_type=AuctionType.SECOND_PRICE,
            latency_ms=round(latency, 2),
        )

    # ─── Private helpers ──────────────────────────────────────────────────────

    async def _get_ad_slot(self, slot_id: str) -> Optional[AdSlot]:
        result = await self.db.execute(
            select(AdSlot).where(AdSlot.id == slot_id, AdSlot.is_active == True)
        )
        return result.scalar_one_or_none()

    async def _get_eligible_campaigns(
        self, bid_request: BidRequest, ad_slot: AdSlot
    ) -> list[Campaign]:
        """
        A campaign is eligible if:
        - Status is ACTIVE
        - Has remaining daily budget
        - Bid floor is above the slot's floor price
        """
        result = await self.db.execute(
            select(Campaign)
            .where(
                and_(
                    Campaign.status == CampaignStatus.ACTIVE,
                    Campaign.spent_today_cents < Campaign.daily_budget_cents,
                    Campaign.max_cpm_cents >= ad_slot.floor_price_cents,
                )
            )
            .options(selectinload(Campaign.creatives))
        )
        campaigns = result.scalars().all()

        # Apply targeting filters
        eligible = []
        for c in campaigns:
            if self._passes_targeting(c, bid_request):
                eligible.append(c)

        return eligible

    def _passes_targeting(self, campaign: Campaign, bid_request: BidRequest) -> bool:
        """
        Check if a campaign's targeting rules match the bid request context.
        Empty targeting list = no restriction (bid on everything).
        """
        try:
            target_countries = json.loads(campaign.target_countries)
            target_devices = json.loads(campaign.target_devices)

            if target_countries and bid_request.country not in target_countries:
                return False

            if target_devices and bid_request.device_type.value not in target_devices:
                return False

        except (json.JSONDecodeError, AttributeError):
            pass

        return True

    def _collect_bids(
        self, campaigns: list[Campaign], ad_slot: AdSlot
    ) -> list[dict]:
        """
        Each campaign bids its max_cpm_cents, capped by remaining budget.
        In a real DSP, bid shading algorithms would reduce bids below max
        to optimise ROI — we keep it simple here.
        """
        bids = []
        for campaign in campaigns:
            # Can't bid more than remaining budget
            bid_amount = min(
                campaign.max_cpm_cents,
                campaign.remaining_daily_budget_cents
            )
            if bid_amount >= ad_slot.floor_price_cents:
                bids.append({
                    "campaign": campaign,
                    "bid_cents": bid_amount,
                })

        # Sort descending by bid amount
        return sorted(bids, key=lambda x: x["bid_cents"], reverse=True)

    def _run_second_price_auction(
        self, bids: list[dict], ad_slot: AdSlot
    ) -> dict:
        """
        Vickrey/second-price auction:
        - Winner = highest bidder
        - Clearing price = second-highest bid + 1 cent
        - If only one bidder, clearing price = floor price + 1 cent

        Why second-price? It's strategy-proof — bidding your true value is
        always the dominant strategy. You never pay more than necessary.
        """
        winner = bids[0]
        highest_bid = winner["bid_cents"]

        if len(bids) >= 2:
            second_price = bids[1]["bid_cents"] + 1
        else:
            # Solo bidder pays floor + 1 cent
            second_price = ad_slot.floor_price_cents + 1

        # Clearing price can't exceed the winner's own bid
        clearing_price = min(second_price, highest_bid)

        return {
            "winner": winner["campaign"],
            "highest_bid_cents": highest_bid,
            "clearing_price_cents": clearing_price,
        }

    async def _pick_creative(self, campaign: Campaign) -> Optional[AdCreative]:
        """Pick first active creative. Could be round-robin for A/B testing."""
        for creative in campaign.creatives:
            if creative.is_active:
                return creative
        return None

    async def _save_bid_records(self, auction_id: str, bids: list[dict]):
        for i, bid in enumerate(bids):
            record = BidRecord(
                auction_id=auction_id,
                campaign_id=bid["campaign"].id,
                bid_price_cents=bid["bid_cents"],
                is_winner=(i == 0),
            )
            self.db.add(record)
        await self.db.flush()

    async def _save_auction_result(
        self, auction_id, ad_slot, winning_campaign,
        creative, highest_bid, clearing_price, num_bidders
    ):
        result = AuctionResult(
            auction_id=auction_id,
            ad_slot_id=ad_slot.id,
            winning_campaign_id=winning_campaign.id,
            winning_creative_id=creative.id if creative else None,
            highest_bid_cents=highest_bid,
            clearing_price_cents=clearing_price,
            num_bidders=num_bidders,
            had_fill=True,
        )
        self.db.add(result)
        await self.db.flush()

    async def _deduct_budget(self, campaign: Campaign, clearing_price_cents: int):
        campaign.spent_today_cents += clearing_price_cents
        campaign.total_spent_cents += clearing_price_cents

        # Check if campaign has exhausted budget
        if campaign.spent_today_cents >= campaign.daily_budget_cents:
            campaign.status = CampaignStatus.EXHAUSTED

        await self.db.flush()

    async def _emit_impression_event(
        self, auction_id, campaign, ad_slot, clearing_price, bid_request
    ):
        """
        Push impression event to Redis Stream.
        Redis Streams are an append-only log — perfect for event pipelines.
        Consumer workers (analytics, billing) read from these streams.
        """
        try:
            # Save impression to DB
            impression = Impression(
                auction_id=auction_id,
                campaign_id=campaign.id,
                ad_slot_id=ad_slot.id,
                clearing_price_cents=clearing_price,
                country=bid_request.country,
                device_type=bid_request.device_type.value,
            )
            self.db.add(impression)
            await self.db.flush()

            # Emit to Redis Stream for real-time consumers
            await self.redis.xadd(IMPRESSION_STREAM, {
                "auction_id": auction_id,
                "campaign_id": campaign.id,
                "ad_slot_id": ad_slot.id,
                "clearing_price_cents": str(clearing_price),
                "country": bid_request.country,
                "device_type": bid_request.device_type.value,
                "timestamp": datetime.utcnow().isoformat(),
            })
        except Exception as e:
            # Never let Redis failures block the auction response
            print(f"Warning: Redis emit failed: {e}")

    async def _save_no_fill_result(self, auction_id, ad_slot, start_time):
        result = AuctionResult(
            auction_id=auction_id,
            ad_slot_id=ad_slot.id,
            highest_bid_cents=0,
            clearing_price_cents=0,
            num_bidders=0,
            had_fill=False,
        )
        self.db.add(result)
        await self.db.flush()

    def _no_fill_response(self, auction_id, bid_request, start_time) -> BidResponse:
        latency = (time.monotonic() - start_time) * 1000
        return BidResponse(
            auction_id=auction_id,
            ad_slot_id=bid_request.ad_slot_id,
            had_fill=False,
            winning_campaign_id=None,
            winning_creative_id=None,
            clearing_price_cents=0,
            highest_bid_cents=0,
            num_bidders=0,
            auction_type=AuctionType.SECOND_PRICE,
            latency_ms=round(latency, 2),
        )
