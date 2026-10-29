"""An OpenRTB 2.5 bidder: the engine's campaigns bidding on an exchange's auction.

On an exchange the roles change. The exchange runs the auction and sets the
price; the bidder only says what it would pay, then learns the outcome from a
win or loss notice, often seconds later and sometimes never. So:

- Bidding draws on a budget lease (app/services/leasing.py). A bid can cost
  at most itself, and that much comes out of a slice of the campaign's
  budget the database already holds for this process. Leases count against
  budgets everywhere (the house auction too), so any number of bids in
  flight, across any number of processes, cannot together overspend.
- A win charges the exchange's clearing price out of its bid's lease. A loss
  or a silent bid (expired after BID_TTL_SECONDS) moves no money: its share
  returns when the lease closes. Each bid settles at most once, under a row
  lock.
- A win notice that arrives after its lease closed is still charged: the ad
  ran and the exchange bills for it. Its lease already gave the money back,
  so that is the one path that can overspend. It is counted, not hidden.

Eligibility, targeting, pacing, the A/B arm and pCTR are the engine's own
code, run against the exchange's impression instead of a house slot.
"""
import html
import json
import logging
import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Optional

from sqlalchemy import (
    BigInteger, Integer, and_, case, cast, func, insert, literal, or_, select, update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.ml.artifacts import load_ctr_model
from app.ml.predictor import predict_ctr
from app.models.models import (
    AdCreative, AuctionResult, BudgetLease, Campaign, CampaignStatus, Click,
    DeviceType, ExchangeBid, Impression, gen_uuid,
)
from app.schemas import openrtb as ortb
from app.schemas.schemas import BidRequest as EngineBidRequest
from app.services.accounting import (
    MICROS_PER_CENT, MICROS_PER_CPM_CENT, cpm_cents_to_impression_micros,
)
from app.services.auction_engine import AuctionEngine
from app.services.contextual import classify_url, domain_of
from app.services.leasing import BOOK, LeaseBook, acquire, close_finished, lease_size
from app.services.pacing import ahead_of_pace

logger = logging.getLogger(__name__)

BID_TTL_SECONDS = float(os.getenv("BID_TTL_SECONDS", "60"))
SEAT = "rtb-engine"

# OpenRTB 2.5 sends ISO-3166-1 alpha-3; campaigns target alpha-2. A country
# not listed here matches no country-targeted campaign, which is the safe way
# to be wrong: run-of-network campaigns still bid.
ALPHA3_TO_ALPHA2 = {
    "USA": "US", "GBR": "GB", "NGA": "NG", "DEU": "DE", "CAN": "CA", "AUS": "AU",
    "FRA": "FR", "BRA": "BR", "IND": "IN", "CHN": "CN", "JPN": "JP", "KOR": "KR",
    "ITA": "IT", "ESP": "ES", "NLD": "NL", "BEL": "BE", "CHE": "CH", "AUT": "AT",
    "SWE": "SE", "NOR": "NO", "DNK": "DK", "FIN": "FI", "IRL": "IE", "PRT": "PT",
    "POL": "PL", "CZE": "CZ", "HUN": "HU", "ROU": "RO", "GRC": "GR", "TUR": "TR",
    "RUS": "RU", "UKR": "UA", "ISR": "IL", "SAU": "SA", "ARE": "AE", "EGY": "EG",
    "ZAF": "ZA", "KEN": "KE", "GHA": "GH", "MAR": "MA", "DZA": "DZ", "TUN": "TN",
    "ETH": "ET", "UGA": "UG", "TZA": "TZ", "CMR": "CM", "CIV": "CI", "SEN": "SN",
    "MEX": "MX", "ARG": "AR", "CHL": "CL", "COL": "CO", "PER": "PE", "VEN": "VE",
    "IDN": "ID", "MYS": "MY", "SGP": "SG", "THA": "TH", "VNM": "VN", "PHL": "PH",
    "PAK": "PK", "BGD": "BD", "NZL": "NZ", "HKG": "HK", "TWN": "TW", "LKA": "LK",
    "NPL": "NP", "QAT": "QA", "KWT": "KW", "BHR": "BH", "OMN": "OM", "JOR": "JO",
    "LBN": "LB", "IRN": "IR", "IRQ": "IQ",
}

# IAB content taxonomy 1.0 tier-1 codes, onto the engine's categories.
IAB_TO_CATEGORY = {
    "IAB1": "entertainment", "IAB3": "finance", "IAB12": "news",
    "IAB13": "finance", "IAB17": "sports", "IAB18": "fashion", "IAB19": "tech",
}
# Non-standard and illegal content: never bid, whatever a campaign targets.
UNSAFE_IAB = {"IAB25", "IAB26"}

# 5.21 device types. Connected TV, connected devices and set-top boxes are
# not display inventory this bidder has creatives for.
DEVICETYPE = {1: "mobile", 2: "desktop", 4: "mobile", 5: "tablet"}
UNSUPPORTED_DEVICETYPES = {3, 6, 7}


def country_alpha2(code: Optional[str]) -> str:
    code = (code or "").strip().upper()
    if len(code) == 2:
        return code
    return ALPHA3_TO_ALPHA2.get(code, "")


def device_of(device: Optional[ortb.Device]) -> Optional[str]:
    """desktop, mobile or tablet; None for a device this bidder does not serve."""
    if device and device.devicetype is not None:
        if device.devicetype in UNSUPPORTED_DEVICETYPES:
            return None
        if device.devicetype in DEVICETYPE:
            return DEVICETYPE[device.devicetype]
    ua = ((device.ua if device else None) or "").lower()
    if "ipad" in ua or "tablet" in ua:
        return "tablet"
    if any(k in ua for k in ("mobile", "iphone", "android")):
        return "mobile"
    return "desktop"


def iab_tier1(codes) -> set[str]:
    return {str(c).split("-", 1)[0].strip().upper() for c in codes or ()}


def category_of(site: Optional[ortb.Site]) -> tuple[Optional[str], str]:
    """The page's category: from the exchange's IAB codes, else from its URL."""
    if site:
        for code in site.cat:
            category = IAB_TO_CATEGORY.get(str(code).split("-", 1)[0].upper())
            if category:
                return category, "iab"
        category, source = classify_url(site.page or site.domain or "")
        if category:
            return category, source
    return None, "unknown"


def floor_cents(imp: ortb.Imp) -> int:
    """The floor as whole CPM cents, rounded up: a bid at it must still clear it."""
    return int((Decimal(str(imp.bidfloor)) * 100).to_integral_value(ROUND_CEILING))


def price_to_micros(price: str) -> int:
    """An ${AUCTION_PRICE} CPM in dollars, as one impression's cost in micros.

    Decimal, not float: "0.3" CPM is exactly 300 micros, and a float would
    make it 299 or 300 depending on the value.
    """
    try:
        value = Decimal(price)
    except (InvalidOperation, TypeError):
        raise ValueError(f"not a price: {price!r}")
    if not value.is_finite() or value < 0:
        raise ValueError(f"not a price: {price!r}")
    return int((value * 1000).to_integral_value(ROUND_HALF_UP))


def banner_size(banner: Optional[ortb.Banner]) -> Optional[tuple[int, int]]:
    if banner is None:
        return None
    if banner.w and banner.h:
        return banner.w, banner.h
    if banner.format:
        return banner.format[0].w, banner.format[0].h
    return None


@dataclass
class ExchangeSlot:
    """The exchange's impression, shaped like the house slot the engine reads."""
    width: int
    height: int
    floor_price_cents: int
    banner_pos: int
    publisher: SimpleNamespace
    id: Optional[str] = None


def advertiser_domain(campaign: Campaign) -> str:
    domain = (campaign.advertiser.domain if campaign.advertiser else "") or ""
    return domain.strip().lower().removeprefix("www.")


def model_version() -> str:
    model = load_ctr_model() or {}
    return f"avazu-lr:{model.get('trained_at', 'baseline')}"


class Bidder:
    def __init__(self, db: AsyncSession, book: LeaseBook = BOOK):
        self.db = db
        self.book = book
        self.engine = AuctionEngine(db, simulate_clicks=False)

    # ── Bidding ──────────────────────────────────────────────────────────────

    async def bid(self, request: ortb.BidRequest, base_url: str) -> Optional[ortb.BidResponse]:
        if request.cur and "USD" not in {c.upper() for c in request.cur}:
            return None
        site = request.site
        if site and iab_tier1(site.cat) & UNSAFE_IAB:
            return None
        device = device_of(request.device)
        if device is None:
            return None

        bids = []
        for imp in request.imp:
            bid = await self._bid_on(request, imp, device, base_url)
            if bid:
                bids.append(bid)
        if not bids:
            return None
        return ortb.BidResponse(id=request.id, bidid=gen_uuid(),
                                seatbid=[ortb.SeatBid(seat=SEAT, bid=bids)])

    async def _bid_on(self, request, imp, device, base_url) -> Optional[ortb.Bid]:
        size = banner_size(imp.banner)
        if size is None or imp.bidfloorcur.upper() != "USD":
            return None
        site = request.site
        category, category_source = category_of(site)
        page = (site.page or (f"https://{site.domain}" if site.domain else "")) if site else ""
        slot = ExchangeSlot(
            width=size[0], height=size[1], floor_price_cents=floor_cents(imp),
            # 5.4: 1 is above the fold. The model's ad positions are Avazu's,
            # whose codes are undocumented: above the fold as 1, else 0.
            banner_pos=1 if (imp.banner.pos == 1) else 0,
            publisher=SimpleNamespace(category=category),
        )
        geo = request.device.geo if request.device else None
        context = EngineBidRequest(
            ad_slot_id="openrtb", country=country_alpha2(geo.country if geo else None),
            device_type=DeviceType(device), page_url=page,
            user_agent=(request.device.ua if request.device else None) or "",
        )

        eligible, excluded = await self.engine._get_eligible_campaigns(context, slot, category)
        if request.badv:
            eligible = self._without_blocked_advertisers(eligible, request.badv)
        if not eligible:
            return None

        strategy = random.choice(["control", "treatment"])
        hour = datetime.now(timezone.utc).hour
        pctr = predict_ctr(device_type=device, publisher_category=category,
                           banner_pos=slot.banner_pos, hour=hour)
        candidates = self.engine._collect_bids(eligible, slot, strategy, pctr, excluded)

        features = {
            "device_type": device, "country": context.country, "category": category,
            "category_source": category_source, "banner_pos": slot.banner_pos,
            "hour": hour, "domain": domain_of(page) if page else None,
            "width": slot.width, "height": slot.height, "floor_cents": slot.floor_price_cents,
            "auction_type": request.at,
            # Not read by the served model. Logged because the iPinYou replay
            # found user segments carry most of the click signal: the next
            # model trained on this log can use them.
            "user_segments": [s.id for d in (request.user.data if request.user else [])
                              for s in d.segment if s.id],
        }
        now = datetime.utcnow()
        for candidate in candidates:
            campaign = candidate["campaign"]
            cost = cpm_cents_to_impression_micros(candidate["bid_cents"])
            if ahead_of_pace(campaign, cost, now):
                continue
            lease_id = await self._lease_for(campaign, cost, now)
            if lease_id is None:
                continue
            creative = self.engine._pick_creative(campaign, slot)
            bid_id = gen_uuid()
            await self._log_bid(bid_id, request, imp, campaign, creative, lease_id,
                                strategy, pctr, features, candidate["bid_cents"], cost, now)
            return self._bid(bid_id, imp, campaign, creative, candidate["bid_cents"], base_url)
        return None

    async def _lease_for(self, campaign: Campaign, cost: int, now: datetime) -> Optional[str]:
        """Room for this bid on a lease: the open one, or a new one if it is full."""
        lease_id = self.book.take(campaign.id, cost, now)
        if lease_id:
            return lease_id
        async with self.book.lock(campaign.id):
            # Another bid may have taken a fresh lease while this one waited.
            lease_id = self.book.take(campaign.id, cost, now)
            if lease_id:
                return lease_id
            amount = lease_size(campaign, cost, now)
            if amount < cost:
                return None
            lease = await acquire(self.db, campaign, amount, now)
            if lease is None:
                return None
            self.book.put(campaign.id, lease)
            return self.book.take(campaign.id, cost, now)

    @staticmethod
    def _without_blocked_advertisers(campaigns, blocked) -> list[Campaign]:
        """Drop campaigns whose advertiser the exchange's badv list blocks."""
        return [c for c in campaigns
                if not AuctionEngine._domain_matches(advertiser_domain(c), blocked)]

    async def _log_bid(self, bid_id, request, imp, campaign, creative, lease_id,
                       strategy, pctr, features, bid_cents, cost, now) -> None:
        """One INSERT: the bid's own row, not the campaign's contended one."""
        await self.db.execute(insert(ExchangeBid).values(
            id=bid_id, request_id=request.id[:64], imp_id=imp.id[:64],
            campaign_id=campaign.id, creative_id=creative.id, lease_id=lease_id,
            strategy=strategy, model_version=model_version(), pctr=float(pctr),
            features=json.dumps(features), bid_cents=bid_cents, max_cost_micros=cost,
            status="pending", clicked=False, created_at=now))

    @staticmethod
    def _bid(bid_id, imp, campaign, creative, bid_cents, base_url) -> ortb.Bid:
        base = base_url.rstrip("/") + "/api/v1/openrtb"
        click = f"{base}/click/{bid_id}"
        adm = (f'<a href="{html.escape(click)}" target="_blank" rel="noopener">'
               f'<img src="{html.escape(creative.asset_url)}" width="{creative.width}" '
               f'height="{creative.height}" alt="" style="border:0"></a>')
        return ortb.Bid(
            id=bid_id, impid=imp.id, price=bid_cents / 100,
            adid=creative.id, crid=creative.id, cid=campaign.id,
            # The advertiser's domain: what the exchange's badv block list is
            # matched against, here and on its side.
            adomain=[advertiser_domain(campaign)],
            w=creative.width, h=creative.height, adm=adm,
            # The exchange substitutes these macros when it calls back.
            nurl=f"{base}/win/{bid_id}?price=${{AUCTION_PRICE}}",
            lurl=f"{base}/loss/{bid_id}?reason=${{AUCTION_LOSS}}",
        )

    # ── Settlement ───────────────────────────────────────────────────────────

    async def _locked_bid(self, bid_id: str) -> Optional[ExchangeBid]:
        return (await self.db.execute(
            select(ExchangeBid).where(ExchangeBid.id == bid_id).with_for_update()
        )).scalar_one_or_none()

    async def settle_win(self, bid_id: str, price_micros: int) -> str:
        """Charge a won bid. Returns charged, late, duplicate or unknown.

        The charge comes out of the bid's lease: the campaign's reservation
        shrinks by what was spent. Late means the lease had already closed and
        given its money back, so the charge has no reservation behind it.
        """
        bid = await self._locked_bid(bid_id)
        if bid is None:
            return "unknown"
        if bid.status == "won":
            return "duplicate"
        cap = cpm_cents_to_impression_micros(bid.bid_cents)
        if price_micros > cap:
            # A second-price exchange never charges above the bid; one that
            # says otherwise is billed at the bid, and it is logged.
            logger.warning("bid=%s win price %s above bid %s; charged the bid",
                           bid_id, price_micros, cap)
        charged = min(price_micros, cap)
        # Locked after the bid, as everywhere: bid, then lease, then campaign.
        lease = await self.db.get(BudgetLease, bid.lease_id, with_for_update=True)
        covered = lease is not None and lease.status == "open"

        features = json.loads(bid.features)
        impression_id = gen_uuid()
        clearing_cents = round(charged / MICROS_PER_CPM_CENT)
        self.db.add(AuctionResult(
            auction_id=bid.id, ad_slot_id=None, source="openrtb",
            winning_campaign_id=bid.campaign_id, winning_creative_id=bid.creative_id,
            highest_bid_cents=bid.bid_cents, clearing_price_cents=clearing_cents,
            charged_cost_micros=charged, num_bidders=1, strategy=bid.strategy,
            had_fill=True))
        self.db.add(Impression(
            id=impression_id, auction_id=bid.id, campaign_id=bid.campaign_id,
            ad_slot_id=None, source="openrtb", clearing_price_cents=clearing_cents,
            charged_cost_micros=charged, country=features.get("country") or None,
            device_type=features.get("device_type")))
        # A click can reach us before the win notice does; it was held on the
        # bid until the impression it belongs to existed. Flushed first: with
        # no ORM relationship between them, the unit of work does not know
        # the click's insert has to wait for the impression's.
        if bid.clicked:
            await self.db.flush()
            self.db.add(Click(impression_id=impression_id, campaign_id=bid.campaign_id))
        bid.status, bid.charged_micros, bid.impression_id = "won", charged, impression_id
        bid.settled_at = datetime.utcnow()
        if covered:
            lease.charged_micros += charged
        await self.db.flush()
        # The campaign last: an UPDATE holds its row lock until COMMIT, and
        # made first it was held across every insert above.
        await self._charge(bid.campaign_id, charged, release=charged if covered else 0)
        if not covered:
            logger.warning("bid=%s won after its lease closed; charged %s without a "
                           "reservation", bid_id, charged)
        return "charged" if covered else "late"

    async def _charge(self, campaign_id: str, charged: int, release: int) -> None:
        """Spend `charged`, out of `release` of reserved. Never refused: the ad ran."""
        today = datetime.now(timezone.utc).date()
        spent_today = AuctionEngine._spent_today_expression(today)
        new_daily = spent_today + charged
        new_total = Campaign.total_spent_micros + charged
        daily_limit = cast(Campaign.daily_budget_cents, BigInteger) * MICROS_PER_CENT
        total_limit = cast(Campaign.total_budget_cents, BigInteger) * MICROS_PER_CENT
        status_type = Campaign.status.type
        await self.db.execute(
            update(Campaign).where(Campaign.id == campaign_id).values(
                spent_today_micros=new_daily,
                total_spent_micros=new_total,
                spend_date=today,
                reserved_micros=Campaign.reserved_micros - release,
                status=case(
                    (or_(new_daily >= daily_limit, new_total >= total_limit),
                     literal(CampaignStatus.EXHAUSTED, type_=status_type)),
                    # A campaign exhausted on an earlier day is a new day's
                    # campaign again; a paused one stays paused.
                    (and_(Campaign.status == CampaignStatus.EXHAUSTED,
                          Campaign.spend_date < today),
                     literal(CampaignStatus.ACTIVE, type_=status_type)),
                    else_=Campaign.status,
                ),
            ).execution_options(synchronize_session=False))

    async def settle_loss(self, bid_id: str, reason: Optional[int]) -> str:
        """Record a loss. Its share of the lease comes back when the lease closes."""
        bid = await self._locked_bid(bid_id)
        if bid is None:
            return "unknown"
        if bid.status != "pending":
            return "duplicate"
        bid.status, bid.loss_reason, bid.settled_at = "lost", reason, datetime.utcnow()
        await self.db.flush()
        return "lost"

    async def sweep(self, now: Optional[datetime] = None,
                    ttl_seconds: float = BID_TTL_SECONDS) -> dict:
        """Expire bids that never heard back, and close finished leases."""
        now = now or datetime.utcnow()
        expired = (await self.db.execute(
            update(ExchangeBid)
            .where(ExchangeBid.status == "pending",
                   ExchangeBid.created_at < now - timedelta(seconds=ttl_seconds))
            .values(status="expired", settled_at=now)
            .execution_options(synchronize_session=False))).rowcount
        closed, released = await close_finished(self.db, now, ttl_seconds)
        return {"expired_bids": expired, "closed_leases": closed, "released_micros": released}

    async def record_click(self, bid_id: str) -> Optional[str]:
        """Count the click once and return where to send the user."""
        bid = await self._locked_bid(bid_id)
        if bid is None:
            return None
        if not bid.clicked:
            bid.clicked = True
            if bid.status == "won" and bid.impression_id:
                self.db.add(Click(impression_id=bid.impression_id, campaign_id=bid.campaign_id))
        await self.db.flush()
        return (await self.db.execute(
            select(AdCreative.click_url).where(AdCreative.id == bid.creative_id))).scalar_one()

    async def stats(self) -> dict:
        rows = (await self.db.execute(
            select(ExchangeBid.status, func.count(), func.coalesce(func.sum(ExchangeBid.charged_micros), 0),
                   func.coalesce(func.sum(cast(ExchangeBid.clicked, Integer)), 0))
            .group_by(ExchangeBid.status))).all()
        by_status = {s: {"bids": n, "charged_micros": int(c), "clicks": int(k)} for s, n, c, k in rows}
        reserved = (await self.db.execute(
            select(func.coalesce(func.sum(Campaign.reserved_micros), 0)))).scalar()
        open_leases, leased = (await self.db.execute(
            select(func.count(), func.coalesce(func.sum(
                BudgetLease.amount_micros - BudgetLease.charged_micros), 0))
            .where(BudgetLease.status == "open"))).one()
        taken = (await self.db.execute(select(func.count()).select_from(BudgetLease))).scalar()
        settled = sum(by_status.get(s, {}).get("bids", 0) for s in ("won", "lost"))
        won = by_status.get("won", {"bids": 0, "charged_micros": 0, "clicks": 0})
        return {
            "by_status": by_status,
            "reserved_micros": int(reserved),
            "leases": {"open": int(open_leases), "open_unspent_micros": int(leased),
                       "taken": int(taken)},
            "win_rate": won["bids"] / settled if settled else None,
            "ecpc_micros": won["charged_micros"] / won["clicks"] if won["clicks"] else None,
            "bid_ttl_seconds": BID_TTL_SECONDS,
        }
