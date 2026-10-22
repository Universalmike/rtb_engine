"""
RTB Auction Engine

How a real auction works (simplified):
1. Publisher page loads → sends bid request with ad slot context
2. Engine fetches all eligible campaigns (active, budget available, targeting match)
3. Each campaign submits a bid (their max CPM)
4. Second-price auction: winner pays 2nd highest bid + 1 cent (Vickrey auction)
5. Budget is charged and the result stored, in one atomic statement
6. Impression event is fired to Redis Stream, after the write has landed

Second-price auction theory: bidders are incentivized to bid their true value
because they never pay more than the second-highest bid. This is the same
mechanism used by Google Ad Exchange, AppNexus, and most major DSPs.
"""

import asyncio
import uuid
import time
import json
import logging
import os
import random
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import (
    BigInteger, Boolean, Integer, String, and_, case, cast, column, func,
    insert, literal, or_, select, true, update,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.attributes import set_committed_value

from app.models.models import (
    Advertiser, Campaign, CampaignStatus, AdSlot, AdCreative,
    BidRecord, AuctionResult, Impression, gen_uuid,
)
from app.schemas.schemas import BidRequest, BidResponse, AuctionType
from app.core.redis_client import get_redis, IMPRESSION_STREAM
from app.ml.predictor import predict_ctr
from app.services.contextual import classify_url, domain_of
logger = logging.getLogger(__name__)

# Phase timings are always logged; set AUCTION_PROFILE=1 to also return them on
# the response, which is how you read them from Swagger without server logs.
PROFILE_AUCTIONS = os.getenv("AUCTION_PROFILE", "").lower() in {"1", "true", "yes"}

from app.services.accounting import (
    MICROS_PER_CENT,
    cpm_cents_to_impression_micros,
    max_affordable_cpm_cents,
)

# Ad slots, with their publisher, keyed by id. A slot's size, floor and
# category are inventory configuration: they change when someone edits the
# site, not between one page view and the next, yet fetching them was a full
# database round trip on every bid request. Bounded staleness is the price;
# writes made through this process clear the cache immediately.
SLOT_CACHE_TTL_SECONDS = float(os.getenv("SLOT_CACHE_TTL_SECONDS", "30"))
_slot_cache: dict[str, tuple[float, AdSlot]] = {}


def clear_slot_cache() -> None:
    """Forget every cached slot. Call after anything that changes inventory."""
    _slot_cache.clear()


# Strong references to in-flight event publishes; the event loop only keeps
# weak ones, and a task collected mid-flight is silently dropped.
_background_tasks: set[asyncio.Task] = set()


class AuctionEngine:

    def __init__(self, db: AsyncSession):
        self.db = db
        self.redis = get_redis()
        # Impression events from auctions this engine has run, held until
        # publish_events() so nothing is announced before its write commits.
        self.pending_events: list[dict] = []

    async def run_auction(self, bid_request: BidRequest) -> BidResponse:
        """Main auction flow, wrapped in per-phase timing.

        This path is I/O bound, not compute bound: a warm auction is two
        sequential database round trips, the eligibility read and the
        charge-and-record write. The per-phase breakdown exists to keep that
        visible: each entry is the ms spent between two steps, so one slow
        trip shows up instead of being buried in a single total.

        perf_counter, not monotonic: on Windows monotonic ticks every 15.6ms,
        which reported every phase as either 0 or 15.6.
        """
        start_time = time.perf_counter()
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
                auction_id, (time.perf_counter() - start_time) * 1000, phases,
            )

        if PROFILE_AUCTIONS:
            response.timings_ms = phases
        return response

    @staticmethod
    def _phase_marker(phases: dict):
        """Returns mark(name): records ms elapsed since the previous mark."""
        previous = [time.perf_counter()]

        def mark(name: str) -> None:
            now = time.perf_counter()
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
            category, category_source = self._context_category(bid_request, None)
            return self._no_fill_response(
                auction_id, bid_request, start_time, "control", excluded,
                category, category_source)

        # 2. Get eligible campaigns
        # Classified once per auction: the page is a property of the request,
        # not of any campaign looking at it.
        category, category_source = self._context_category(bid_request, ad_slot)
        eligible_campaigns, excluded = await self._get_eligible_campaigns(
            bid_request, ad_slot, category)
        mark("eligibility_query")

        if not eligible_campaigns:
            await self._save_no_fill_result(auction_id, ad_slot, start_time, "control")
            return self._no_fill_response(
                auction_id, bid_request, start_time, "control", excluded,
                category, category_source)

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
                auction_id, bid_request, start_time, strategy, excluded,
                category, category_source)

        # 4. Run the winner's configured auction type, then charge it and
        # record the auction in one statement: the budget reservation, the
        # auction result, every bid record and the impression either all land
        # or none do. A concurrent auction may spend a candidate's last budget
        # after eligibility was read; then nothing is written, the candidate
        # is removed, and the auction reruns.
        result = None
        charged_cost_micros = 0
        creative = None
        while bids:
            candidate_result = self._run_price_auction(bids, ad_slot)
            candidate = candidate_result["winner"]
            candidate_cost = cpm_cents_to_impression_micros(
                candidate_result["clearing_price_cents"]
            )
            candidate_creative = self._pick_creative(candidate, ad_slot)
            records = self._auction_records(
                auction_id, ad_slot, bid_request, bids, candidate_result,
                candidate_creative, candidate_cost, strategy,
            )
            if await self._reserve_budget(candidate, candidate_cost, records):
                result = candidate_result
                charged_cost_micros = candidate_cost
                creative = candidate_creative
                break
            bids = [bid for bid in bids if bid["campaign"].id != candidate.id]
        mark("reserve_and_record")

        if result is None:
            await self._save_no_fill_result(auction_id, ad_slot, start_time, strategy)
            return self._no_fill_response(
                auction_id, bid_request, start_time, strategy, excluded,
                category, category_source)

        winning_campaign = result["winner"]
        clearing_price = result["clearing_price_cents"]

        # 5. Queue the impression event. The caller publishes it once the
        # write is durable; announcing an impression that a failed commit
        # then erased would have consumers bill for an ad nobody bought.
        self._queue_impression_event(
            auction_id, winning_campaign, ad_slot, clearing_price,
            charged_cost_micros, bid_request,
        )

        latency = (time.perf_counter() - start_time) * 1000

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
            page_category=category,
            page_category_source=category_source,
            auction_type=winning_campaign.auction_type,
            latency_ms=round(latency, 2),
            strategy=strategy,
        )

    # ─── Private helpers ──────────────────────────────────────────────────────

    async def _get_ad_slot(self, slot_id: str) -> Optional[AdSlot]:
        """The active slot with its publisher, from cache when fresh.

        Only hits are cached. An unknown id is cheap to look up again, and
        caching the miss would hide a slot created a moment later.
        """
        now = time.monotonic()
        cached = _slot_cache.get(slot_id)
        if cached and cached[0] > now:
            return cached[1]

        result = await self.db.execute(
            select(AdSlot)
            .where(AdSlot.id == slot_id, AdSlot.is_active == True)
            .options(joinedload(AdSlot.publisher))
        )
        slot = result.scalar_one_or_none()
        if slot is not None and SLOT_CACHE_TTL_SECONDS > 0:
            # Detach before sharing. A rollback expires everything still in
            # its session, and an expired attribute on a cached object would
            # fail to load in whichever later request touched it next.
            # Expunge does not cascade to the publisher, so it goes too.
            if slot.publisher is not None:
                self.db.expunge(slot.publisher)
            self.db.expunge(slot)
            _slot_cache[slot_id] = (now + SLOT_CACHE_TTL_SECONDS, slot)
        return slot

    async def _get_eligible_campaigns(
        self, bid_request: BidRequest, ad_slot: AdSlot,
        page_category: Optional[str] = None,
    ) -> tuple[list[Campaign], dict[str, int]]:
        """Campaigns that may bid, and a count of why the rest may not.

        Solvency, status and date filtering happen in SQL, so campaigns
        rejected there are never seen here and are not counted. What is
        counted is targeting, which is the part a visitor changing the bid
        request can actually influence.
        """
        campaigns = await self._query_solvent_campaigns(ad_slot)
        if page_category is None:
            page_category, _ = self._context_category(bid_request, ad_slot)

        eligible: list[Campaign] = []
        excluded: dict[str, int] = {}
        for c in campaigns:
            reason = self._targeting_rejection(
                c, bid_request, ad_slot, page_category)
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
            # Only creatives that could serve this slot. Loading all of them
            # returned every campaign once per creative of every size, and
            # building ORM objects for rows _pick_creative then discards was
            # the single largest CPU cost of an auction. It is an outer join,
            # so a campaign with no fitting creative still comes back, with
            # an empty list, and is still counted under "creative".
            .options(joinedload(Campaign.creatives.and_(
                AdCreative.is_active == True,
                AdCreative.width == ad_slot.width,
                AdCreative.height == ad_slot.height,
            )))
            # A session that already holds a campaign keeps its loaded state
            # by default: here, a creatives list filtered for whichever slot
            # size was asked about first, and the spend as of then. The
            # seeder runs every auction through one session, and without
            # this its fill rate fell from ~96% to ~18%.
            .execution_options(populate_existing=True)
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

    def _context_category(self, bid_request: BidRequest, ad_slot: AdSlot):
        """The content category this impression is sold against, and its source.

        The page wins over the publisher when it can be classified: a page is
        what an advertiser is buying, and publishers have sections. Falling
        back to the publisher's own category is the previous behaviour, so a
        URL that classifies to nothing changes nothing.
        """
        category, source = classify_url(bid_request.page_url or "")
        if category:
            return category, source
        publisher = ad_slot.publisher if ad_slot is not None else None
        return (publisher.category if publisher else None), "publisher"

    def _passes_targeting(
        self, campaign: Campaign, bid_request: BidRequest, ad_slot: AdSlot
    ) -> bool:
        """Whether every targeting rule this campaign sets matches the request."""
        return self._targeting_rejection(campaign, bid_request, ad_slot) is None

    def _targeting_rejection(
        self, campaign: Campaign, bid_request: BidRequest, ad_slot: AdSlot,
        page_category: Optional[str] = None,
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
        if page_category is None:
            page_category, _ = self._context_category(bid_request, ad_slot)

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

            if target_categories and page_category not in target_categories:
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

    _domain_of = staticmethod(domain_of)

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

    def _auction_records(
        self, auction_id: str, ad_slot: AdSlot, bid_request: BidRequest,
        bids: list[dict], result: dict, creative: Optional[AdCreative],
        charged_cost_micros: int, strategy: str,
    ) -> list:
        """INSERTs recording a filled auction, to ride along with its charge.

        Each is a builder taking the reservation CTE and returning an INSERT
        CTE that selects from it. A reservation that matches no row yields no
        rows to insert, so the auction is recorded exactly when it is paid
        for. Every value is typed explicitly: an untyped parameter in an
        INSERT ... SELECT has no target column to infer its type from.
        """
        now = datetime.utcnow()
        winner = result["winner"]

        def typed(col, value):
            return literal(value, type_=col.type)

        def auction_result(reserved):
            row = {
                AuctionResult.id: gen_uuid(),
                AuctionResult.auction_id: auction_id,
                AuctionResult.ad_slot_id: ad_slot.id,
                AuctionResult.winning_campaign_id: winner.id,
                AuctionResult.winning_creative_id: creative.id if creative else None,
                AuctionResult.highest_bid_cents: result["highest_bid_cents"],
                AuctionResult.clearing_price_cents: result["clearing_price_cents"],
                AuctionResult.charged_cost_micros: charged_cost_micros,
                AuctionResult.num_bidders: len(bids),
                AuctionResult.strategy: strategy,
                AuctionResult.had_fill: True,
                AuctionResult.created_at: now,
            }
            return insert(AuctionResult).from_select(
                [c.key for c in row],
                select(*(typed(c, v) for c, v in row.items())).select_from(reserved),
            ).cte("recorded_result")

        def impression(reserved):
            row = {
                Impression.id: gen_uuid(),
                Impression.auction_id: auction_id,
                Impression.campaign_id: winner.id,
                Impression.ad_slot_id: ad_slot.id,
                Impression.clearing_price_cents: result["clearing_price_cents"],
                Impression.charged_cost_micros: charged_cost_micros,
                Impression.country: bid_request.country,
                Impression.device_type: bid_request.device_type.value,
                Impression.created_at: now,
            }
            return insert(Impression).from_select(
                [c.key for c in row],
                select(*(typed(c, v) for c, v in row.items())).select_from(reserved),
            ).cte("recorded_impression")

        def bid_records(reserved):
            # Every bid, win or lose: the audit trail for billing disputes.
            # Passed as one array per column and unnested, not as a VALUES
            # list: the SQL text then stays the same whatever the bid count,
            # so it is compiled and prepared once. A VALUES list is a new
            # statement for every bidder count and opts out of SQLAlchemy's
            # compiled cache entirely, which cost more than the database did.
            submitted = func.unnest(
                literal([gen_uuid() for _ in bids], ARRAY(String)),
                literal([bid["campaign"].id for bid in bids], ARRAY(String)),
                literal([bid["bid_cents"] for bid in bids], ARRAY(Integer)),
                literal([i == 0 for i in range(len(bids))], ARRAY(Boolean)),
            ).table_valued(
                column("id", String), column("campaign_id", String),
                column("bid_price_cents", Integer), column("is_winner", Boolean),
                name="submitted",
            ).render_derived()
            return insert(BidRecord).from_select(
                ["id", "auction_id", "campaign_id", "bid_price_cents",
                 "is_winner", "created_at"],
                select(
                    submitted.c.id,
                    typed(BidRecord.auction_id, auction_id),
                    submitted.c.campaign_id,
                    submitted.c.bid_price_cents,
                    submitted.c.is_winner,
                    typed(BidRecord.created_at, now),
                ).select_from(submitted.join(reserved, true())),
            ).cte("recorded_bids")

        return [auction_result, impression, bid_records]

    async def _reserve_budget(
        self, campaign: Campaign, charge_micros: int, records=(),
    ) -> bool:
        """Atomically reserve spend without crossing daily or lifetime limits.

        This single UPDATE also performs the campaign's daily rollover: when
        spend_date is stale, today's spend starts from zero and the row is
        stamped with today's date as it is charged. Keeping the reset in the
        same conditional UPDATE makes it atomic per row, so concurrent
        auctions cannot race it into double-spending.

        `records` (see _auction_records) are written in the same statement,
        as data-modifying CTEs reading from the reservation. One statement is
        one round trip, is atomic without an explicit transaction, and holds
        the campaign's row lock only while it executes. Charging first and
        inserting afterwards held that lock across every later round trip up
        to COMMIT, so concurrent auctions won by the same campaign queued
        behind each other.
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

        reservation = (
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
        )

        if records:
            reserved = reservation.cte("reserved")
            statement = select(
                reserved.c.spent_today_micros,
                reserved.c.total_spent_micros,
                reserved.c.status,
            ).add_cte(*(build(reserved) for build in records))
        else:
            statement = reservation.execution_options(synchronize_session=False)

        row = (await self.db.execute(statement)).one_or_none()
        if row is None:
            return False

        # The database already holds these values. Assigning them as ordinary
        # attributes would mark the campaign dirty and have the next flush
        # write them straight back: a redundant UPDATE, a round trip, and in a
        # transaction a longer hold on the row lock.
        set_committed_value(campaign, "spent_today_micros", row.spent_today_micros)
        set_committed_value(campaign, "total_spent_micros", row.total_spent_micros)
        set_committed_value(campaign, "status", row.status)
        return True

    def _queue_impression_event(
        self, auction_id, campaign, ad_slot, clearing_price,
        charged_cost_micros, bid_request
    ):
        """Hold an impression event for publish_events()."""
        if not hasattr(self, "pending_events"):
            self.pending_events = []
        self.pending_events.append({
            "auction_id": auction_id,
            "campaign_id": campaign.id,
            "ad_slot_id": ad_slot.id,
            "clearing_price_cents": str(clearing_price),
            "charged_cost_micros": str(charged_cost_micros),
            "country": bid_request.country,
            "device_type": bid_request.device_type.value,
            "timestamp": datetime.utcnow().isoformat(),
        })

    def publish_events(self) -> None:
        """Push queued impression events to the Redis Stream, in the background.

        Call only once the auction's write is durable. Redis Streams are an
        append-only log that consumer workers (analytics, billing) read from;
        none of them is the bidder, so the response does not wait on the
        publish, and a Redis outage costs events, never auctions.
        """
        events, self.pending_events = getattr(self, "pending_events", []), []
        if not events:
            return
        task = asyncio.create_task(self._publish(events))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    async def _publish(self, events: list[dict]) -> None:
        # One pipeline, one connection, one round trip, however many events:
        # a publish per event let a batch (the seeder queues 120) exhaust the
        # Redis connection pool.
        try:
            async with self.redis.pipeline(transaction=False) as pipe:
                for event in events:
                    pipe.xadd(IMPRESSION_STREAM, event)
                await pipe.execute()
        except Exception as e:
            logger.warning("Redis emit failed for %d impression event(s): %s",
                           len(events), e)

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
        self, auction_id, bid_request, start_time, strategy, excluded=None,
        page_category=None, page_category_source="unknown",
    ) -> BidResponse:
        latency = (time.perf_counter() - start_time) * 1000
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
            page_category=page_category,
            page_category_source=page_category_source,
        )
