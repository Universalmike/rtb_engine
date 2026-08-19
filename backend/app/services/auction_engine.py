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
import logging
import os
import random
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import BigInteger, and_, case, cast, literal, or_, select, update
from sqlalchemy.orm import joinedload

from app.models.models import (
    Advertiser, Campaign, CampaignStatus, AdSlot, AdCreative,
    BidRecord, AuctionResult, Impression,
)
from app.schemas.schemas import BidRequest, BidResponse, AuctionType
from app.core.redis_client import get_redis, IMPRESSION_STREAM
from app.ml.predictor import predict_ctr
logger = logging.getLogger(__name__)

# Phase timings are always logged; set AUCTION_PROFILE=1 to also return them on
# the response, which is how you read them from Swagger without server logs.
PROFILE_AUCTIONS = os.getenv("AUCTION_PROFILE", "").lower() in {"1", "true", "yes"}

from app.services.accounting import (
    MICROS_PER_CENT,
    cpm_cents_to_impression_micros,
    max_affordable_cpm_cents,
)

class AuctionEngine:

    def __init__(self, db: AsyncSession):
        self.db = db
        self.redis = get_redis()

    async def run_auction(self, bid_request: BidRequest) -> BidResponse:
        """Main auction flow, wrapped in per-phase timing.

        This path is I/O bound, not compute bound. A warm auction on the
        deployed stack measures ~150ms end-to-end, of which ~0.1ms is compute;
        the rest is four sequential database round trips. The per-phase
        breakdown exists to keep that visible: each entry is the ms spent
        between two steps, so one slow trip shows up instead of being buried
        in a single total.
        """
        start_time = time.monotonic()
        auction_id = str(uuid.uuid4())
        phases: dict[str, float] = {}
        mark = self._phase_marker(phases)

        try:
            response = await self._execute_auction(
                auction_id, bid_request, start_time, mark
            )
        finally:
            logger.info(
                "auction=%s total_ms=%.2f phases_ms=%s",
                auction_id, (time.monotonic() - start_time) * 1000, phases,
            )

        if PROFILE_AUCTIONS:
            response.timings_ms = phases
        return response

    @staticmethod
    def _phase_marker(phases: dict):
        """Returns mark(name): records ms elapsed since the previous mark."""
        previous = [time.monotonic()]

        def mark(name: str) -> None:
            now = time.monotonic()
            phases[name] = round((now - previous[0]) * 1000, 2)
            previous[0] = now

        return mark

    async def _execute_auction(
        self, auction_id: str, bid_request: BidRequest,
        start_time: float, mark,
    ) -> BidResponse:
        # 1. Fetch the ad slot
        excluded: dict[str, int] = {}

        ad_slot = await self._get_ad_slot(bid_request.ad_slot_id)
        mark("fetch_slot")
        if not ad_slot:
            return self._no_fill_response(
                auction_id, bid_request, start_time, "control", excluded)

        # 2. Get eligible campaigns
        eligible_campaigns, excluded = await self._get_eligible_campaigns(
            bid_request, ad_slot)
        mark("eligibility_query")

        if not eligible_campaigns:
            await self._save_no_fill_result(auction_id, ad_slot, start_time, "control")
            return self._no_fill_response(
                auction_id, bid_request, start_time, "control", excluded)

        # A/B: assign this auction to a bidding strategy and predict CTR once
        # (pCTR is a property of the impression context, identical across
        # campaigns in the same auction).
        strategy = random.choice(["control", "treatment"])
        pctr = predict_ctr(
            device_type=bid_request.device_type.value,
            publisher_category=ad_slot.publisher.category,
            banner_pos=ad_slot.banner_pos,
            hour=datetime.utcnow().hour,
        )
        mark("predict_ctr")

        # 3. Collect bids under the assigned strategy
        bids = self._collect_bids(
            eligible_campaigns, ad_slot, strategy, pctr, excluded)
        mark("collect_bids")

        if not bids:
            await self._save_no_fill_result(auction_id, ad_slot, start_time, strategy)
            return self._no_fill_response(
                auction_id, bid_request, start_time, strategy, excluded)

        # 4. Run the winner's configured auction type and atomically reserve
        # the exact per-impression
        # charge. A concurrent auction may spend a candidate's last budget
        # after eligibility was read; in that case remove it and rerun.
        result = None
        charged_cost_micros = 0
        while bids:
            candidate_result = self._run_price_auction(bids, ad_slot)
            candidate = candidate_result["winner"]
            candidate_cost = cpm_cents_to_impression_micros(
                candidate_result["clearing_price_cents"]
            )
            if await self._reserve_budget(candidate, candidate_cost):
                result = candidate_result
                charged_cost_micros = candidate_cost
                break
            bids = [bid for bid in bids if bid["campaign"].id != candidate.id]
        mark("reserve_budget")

        if result is None:
            await self._save_no_fill_result(auction_id, ad_slot, start_time, strategy)
            return self._no_fill_response(
                auction_id, bid_request, start_time, strategy, excluded)

        winning_campaign = result["winner"]
        clearing_price = result["clearing_price_cents"]

        # 5. Pick a creative from the winning campaign
        creative = self._pick_creative(winning_campaign, ad_slot)

        # 6. Save bid records (all bids, win/lose)
        await self._save_bid_records(auction_id, bids)
        mark("save_bid_records")

        # 7. Save auction result
        await self._save_auction_result(
            auction_id, ad_slot, winning_campaign, creative,
            result["highest_bid_cents"], clearing_price, charged_cost_micros,
            len(bids), strategy
        )
        mark("save_auction_result")

        # 8. Save the impression and emit its event.
        await self._emit_impression_event(
            auction_id, winning_campaign, ad_slot, clearing_price,
            charged_cost_micros, bid_request
        )
        mark("emit_impression")

        # Bid records, auction result and impression were queued, not written.
        # One flush sends them together: three round trips become one. It stays
        # inside the timed section so latency_ms keeps reporting real work.
        await self.db.flush()
        mark("persist")

        latency = (time.monotonic() - start_time) * 1000

        return BidResponse(
            auction_id=auction_id,
            ad_slot_id=bid_request.ad_slot_id,
            had_fill=True,
            winning_campaign_id=winning_campaign.id,
            winning_creative_id=creative.id if creative else None,
            clearing_price_cents=clearing_price,
            charged_cost_micros=charged_cost_micros,
            highest_bid_cents=result["highest_bid_cents"],
            num_bidders=len(bids),
            excluded=excluded,
            auction_type=winning_campaign.auction_type,
            latency_ms=round(latency, 2),
            strategy=strategy,
        )

    # ─── Private helpers ──────────────────────────────────────────────────────

    async def _get_ad_slot(self, slot_id: str) -> Optional[AdSlot]:
        result = await self.db.execute(
            select(AdSlot)
            .where(AdSlot.id == slot_id, AdSlot.is_active == True)
            .options(joinedload(AdSlot.publisher))
        )
        return result.scalar_one_or_none()

    async def _get_eligible_campaigns(
        self, bid_request: BidRequest, ad_slot: AdSlot
    ) -> tuple[list[Campaign], dict[str, int]]:
        """Campaigns that may bid, and a count of why the rest may not.

        Solvency, status and date filtering happen in SQL, so campaigns
        rejected there are never seen here and are not counted. What is
        counted is targeting, which is the part a visitor changing the bid
        request can actually influence.
        """
        campaigns = await self._query_solvent_campaigns(ad_slot)

        eligible: list[Campaign] = []
        excluded: dict[str, int] = {}
        for c in campaigns:
            reason = self._targeting_rejection(c, bid_request, ad_slot)
            if reason is None:
                eligible.append(c)
            else:
                excluded[reason] = excluded.get(reason, 0) + 1

        return eligible, excluded

    async def _query_solvent_campaigns(self, ad_slot: AdSlot) -> list[Campaign]:
        """Active campaigns with budget left that can clear the slot's floor.

        A campaign whose spend_date predates today has spent nothing *today*,
        so its daily spend is zeroed inline rather than by rewriting the row
        first. For the same reason a campaign that exhausted its daily budget
        on an earlier day is admitted again here: the new day resets it.
        """
        now = datetime.utcnow()
        today = datetime.now(timezone.utc).date()
        minimum_charge = cpm_cents_to_impression_micros(ad_slot.floor_price_cents)
        spent_today = self._spent_today_expression(today)
        result = await self.db.execute(
            select(Campaign)
            .join(Advertiser, Campaign.advertiser_id == Advertiser.id)
            .where(
                and_(
                    or_(
                        Campaign.status == CampaignStatus.ACTIVE,
                        and_(
                            Campaign.status == CampaignStatus.EXHAUSTED,
                            Campaign.spend_date < today,
                        ),
                    ),
                    Advertiser.is_active == True,
                    Campaign.start_date <= now,
                    or_(Campaign.end_date.is_(None), Campaign.end_date > now),
                    spent_today + minimum_charge
                    <= cast(Campaign.daily_budget_cents, BigInteger)
                    * MICROS_PER_CENT,
                    Campaign.total_spent_micros + minimum_charge
                    <= cast(Campaign.total_budget_cents, BigInteger)
                    * MICROS_PER_CENT,
                    Campaign.max_cpm_cents >= ad_slot.floor_price_cents,
                )
            )
            .options(joinedload(Campaign.creatives))
        )
        # joinedload on a collection repeats the parent row per child, so the
        # identity map has to de-duplicate before we materialise the list.
        return list(result.unique().scalars().all())

    @staticmethod
    def _spent_today_expression(today):
        """Daily spend as of `today`, treating a stale spend_date as zero.

        Rollover used to be a table-wide UPDATE issued before every auction,
        purely so this could be read as a plain column: a write, and row
        locks, on the hot path of every bid request. Expressed as a CASE the
        read path stays read-only; _reserve_budget persists the reset.
        """
        return case(
            (Campaign.spend_date == today, Campaign.spent_today_micros),
            else_=literal(0, type_=BigInteger),
        )

    def _passes_targeting(
        self, campaign: Campaign, bid_request: BidRequest, ad_slot: AdSlot
    ) -> bool:
        """Whether every targeting rule this campaign sets matches the request."""
        return self._targeting_rejection(campaign, bid_request, ad_slot) is None

    def _targeting_rejection(
        self, campaign: Campaign, bid_request: BidRequest, ad_slot: AdSlot
    ) -> Optional[str]:
        """Name of the first rule the request fails, or None if it passes.

        The name is reported on the bid response. A visitor who changes the
        page URL and watches the bidder count stay flat cannot otherwise tell
        a rule that did not fire from a field being ignored, which this one
        genuinely was until domain targeting existed.

        Empty targeting list = no restriction. Malformed JSON is treated the
        same way: bad data must not silently drop a campaign out of every
        auction it would otherwise have contested.
        """
        try:
            target_countries = json.loads(campaign.target_countries)
            target_devices = json.loads(campaign.target_devices)
            target_categories = json.loads(campaign.target_categories)
            target_domains = json.loads(campaign.target_domains or "[]")
            blocked_domains = json.loads(campaign.blocked_domains or "[]")

            if target_countries and bid_request.country not in target_countries:
                return "country"

            if target_devices and bid_request.device_type.value not in target_devices:
                return "device"

            if target_categories and ad_slot.publisher.category not in target_categories:
                return "category"

            if target_domains or blocked_domains:
                domain = self._domain_of(bid_request.page_url)

                # Brand safety is a veto: an advertiser that will not appear
                # next to a domain does not appear there for any other reason.
                if domain and self._domain_matches(domain, blocked_domains):
                    return "blocked_domain"

                # Contextual targeting that cannot be verified is not a match:
                # a URL we could not read is not evidence of the right page.
                if target_domains and not (
                    domain and self._domain_matches(domain, target_domains)
                ):
                    return "domain"

        except (json.JSONDecodeError, AttributeError):
            return None

        return None

    @staticmethod
    def _domain_of(page_url: str) -> Optional[str]:
        """Host of a page URL, or None when there isn't a usable one.

        'www.' is dropped because no buyer means it, and the port is not part
        of the domain. A bare 'example.com/x' is accepted as well as a full
        URL, since publishers are inconsistent about sending the scheme.
        """
        try:
            parts = urlsplit(page_url if "//" in page_url else f"//{page_url}")
            host = (parts.hostname or "").lower()
        except ValueError:
            return None
        if not host or "." not in host or " " in host:
            return None
        return host[4:] if host.startswith("www.") else host

    @staticmethod
    def _domain_matches(domain: str, entries) -> bool:
        """Whether `domain` is, or sits under, any entry.

        Buying 'example.com' buys 'sports.example.com' — a media buyer means
        the publisher, not one hostname. The dot in the suffix test is what
        stops 'example.com' from also matching 'notexample.com'.
        """
        for entry in entries or ():
            entry = str(entry).strip().lower().lstrip(".")
            if entry.startswith("www."):
                entry = entry[4:]
            if entry and (domain == entry or domain.endswith("." + entry)):
                return True
        return False

    def _collect_bids(
        self, campaigns: list[Campaign], ad_slot: AdSlot,
        strategy: str, pctr: float, excluded: Optional[dict] = None,
    ) -> list[dict]:
        """Each campaign submits a bid.

        control:   flat max CPM (the original behaviour).
        treatment: expected value = pCTR * value_per_click, capped by the
                   advertiser's max CPM. This is how real DSPs derive a CPM bid.
                   value_per_click is in millicents, so pCTR * millicents already
                   yields a CPM in cents (the /1000 for millicents and the *1000
                   for per-impression -> per-mille cancel).
        Both are capped by remaining budget and must clear the slot floor.
        """
        bids = []
        counts = excluded if excluded is not None else {}
        for campaign in campaigns:
            if self._pick_creative(campaign, ad_slot) is None:
                counts["creative"] = counts.get("creative", 0) + 1
                continue
            remaining_micros = min(
                campaign.remaining_daily_budget_micros,
                campaign.remaining_total_budget_micros,
            )
            affordable_cpm = max_affordable_cpm_cents(remaining_micros)
            if strategy == "treatment":
                ev_cpm = round(pctr * campaign.value_per_click_millicents)
                bid_amount = min(
                    ev_cpm, campaign.max_cpm_cents,
                    affordable_cpm,
                )
            else:
                bid_amount = min(
                    campaign.max_cpm_cents,
                    affordable_cpm,
                )
            if bid_amount >= ad_slot.floor_price_cents:
                bids.append({"campaign": campaign, "bid_cents": bid_amount})
            else:
                # Wanted the impression but could not afford the floor, either
                # from its own cap or from what is left of its budget.
                counts["floor"] = counts.get("floor", 0) + 1

        return sorted(bids, key=lambda x: x["bid_cents"], reverse=True)

    def _run_price_auction(
        self, bids: list[dict], ad_slot: AdSlot
    ) -> dict:
        """
        Run the winner's configured price rule:
        - First price: the winner pays its own bid.
        - Second price: the winner pays second-highest bid + 1 cent.
        - A solo second-price bidder pays floor + 1 cent.

        The final clearing price is always capped by the winner's own bid.
        """
        winner = bids[0]
        highest_bid = winner["bid_cents"]

        if winner["campaign"].auction_type == AuctionType.FIRST_PRICE:
            second_price = highest_bid
        elif len(bids) >= 2:
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

    def _pick_creative(
        self, campaign: Campaign, ad_slot: AdSlot
    ) -> Optional[AdCreative]:
        """Pick an active creative whose dimensions match the requested slot."""
        for creative in campaign.creatives:
            if (
                creative.is_active
                and creative.width == ad_slot.width
                and creative.height == ad_slot.height
            ):
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

    async def _save_auction_result(
        self, auction_id, ad_slot, winning_campaign,
        creative, highest_bid, clearing_price, charged_cost_micros,
        num_bidders, strategy
    ):
        result = AuctionResult(
            auction_id=auction_id,
            ad_slot_id=ad_slot.id,
            winning_campaign_id=winning_campaign.id,
            winning_creative_id=creative.id if creative else None,
            highest_bid_cents=highest_bid,
            clearing_price_cents=clearing_price,
            charged_cost_micros=charged_cost_micros,
            num_bidders=num_bidders,
            had_fill=True,
            strategy=strategy,
        )
        self.db.add(result)

    async def _reserve_budget(self, campaign: Campaign, charge_micros: int) -> bool:
        """Atomically reserve spend without crossing daily or lifetime limits.

        This single UPDATE also performs the campaign's daily rollover: when
        spend_date is stale, today's spend starts from zero and the row is
        stamped with today's date as it is charged. Keeping the reset in the
        same conditional UPDATE makes it atomic per row, so concurrent
        auctions cannot race it into double-spending.
        """
        today = datetime.now(timezone.utc).date()
        spent_today = self._spent_today_expression(today)
        new_daily_spend = spent_today + charge_micros
        new_total_spend = Campaign.total_spent_micros + charge_micros
        daily_limit = (
            cast(Campaign.daily_budget_cents, BigInteger) * MICROS_PER_CENT
        )
        total_limit = (
            cast(Campaign.total_budget_cents, BigInteger) * MICROS_PER_CENT
        )

        reservation = await self.db.execute(
            update(Campaign)
            .where(
                Campaign.id == campaign.id,
                or_(
                    Campaign.status == CampaignStatus.ACTIVE,
                    and_(
                        Campaign.status == CampaignStatus.EXHAUSTED,
                        Campaign.spend_date < today,
                    ),
                ),
                new_daily_spend <= daily_limit,
                new_total_spend <= total_limit,
            )
            .values(
                spent_today_micros=new_daily_spend,
                total_spent_micros=new_total_spend,
                spend_date=today,
                status=case(
                    (or_(new_daily_spend >= daily_limit,
                         new_total_spend >= total_limit),
                     literal(
                         CampaignStatus.EXHAUSTED,
                         type_=Campaign.status.type,
                     )),
                    else_=literal(
                        CampaignStatus.ACTIVE,
                        type_=Campaign.status.type,
                    ),
                ),
            )
            .returning(
                Campaign.spent_today_micros,
                Campaign.total_spent_micros,
                Campaign.status,
            )
            .execution_options(synchronize_session=False)
        )
        row = reservation.one_or_none()
        if row is None:
            return False

        campaign.spent_today_micros = row.spent_today_micros
        campaign.total_spent_micros = row.total_spent_micros
        campaign.status = row.status
        return True

    async def _emit_impression_event(
        self, auction_id, campaign, ad_slot, clearing_price,
        charged_cost_micros, bid_request
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
                charged_cost_micros=charged_cost_micros,
                country=bid_request.country,
                device_type=bid_request.device_type.value,
            )
            self.db.add(impression)

            # Emit to Redis Stream for real-time consumers
            await self.redis.xadd(IMPRESSION_STREAM, {
                "auction_id": auction_id,
                "campaign_id": campaign.id,
                "ad_slot_id": ad_slot.id,
                "clearing_price_cents": str(clearing_price),
                "charged_cost_micros": str(charged_cost_micros),
                "country": bid_request.country,
                "device_type": bid_request.device_type.value,
                "timestamp": datetime.utcnow().isoformat(),
            })
        except Exception as e:
            # Never let Redis failures block the auction response
            print(f"Warning: Redis emit failed: {e}")

    async def _save_no_fill_result(self, auction_id, ad_slot, start_time, strategy):
        result = AuctionResult(
            auction_id=auction_id,
            ad_slot_id=ad_slot.id,
            highest_bid_cents=0,
            clearing_price_cents=0,
            charged_cost_micros=0,
            num_bidders=0,
            had_fill=False,
            strategy=strategy,
        )
        self.db.add(result)
        await self.db.flush()

    def _no_fill_response(
        self, auction_id, bid_request, start_time, strategy, excluded=None
    ) -> BidResponse:
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
            strategy=strategy,
            excluded=excluded or {},
        )
