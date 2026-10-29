"""Budget leasing: budget for exchange bids, taken in slices, not per bid.

Reserving each bid on the campaign's row made that row the bottleneck: one
run-of-network campaign was the top bidder on nearly every request, so every
bid's UPDATE queued behind the last one's commit (29 of 44 sampled backends
waiting on its lock). Instead a process leases a slice of the budget, one
write to the row, and admits bids against the slice in memory.

The guarantee is unchanged. A bid's maximum cost always comes out of a lease
the database already holds, and a lease only exists if it fit the budget, so
bids in flight across every process together cannot overspend.

The slice is never refilled: a bid's share stays used even if it loses. The
notice may reach a different process from the one that bid, which then
cannot tell the owner its money is free again. The unused rest comes back
when the lease closes: no new bids after LEASE_SECONDS, then closed by any
process's sweeper once the last of them is past its notice TTL. A lease is
at most half of what the campaign has left, so one process cannot sit on a
campaign's whole budget while others go without.
"""
import asyncio
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import BigInteger, and_, cast, insert, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import BudgetLease, Campaign, CampaignStatus
from app.services.accounting import MICROS_PER_CENT
from app.services.auction_engine import AuctionEngine
from app.services.pacing import allowed_spend_micros

LEASE_FRACTION = float(os.getenv("LEASE_FRACTION", "0.01"))
LEASE_SECONDS = float(os.getenv("LEASE_SECONDS", "10"))
# Who holds a lease, for the logs and for a human reading the table.
OWNER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"[:100]


@dataclass
class LocalLease:
    id: str
    capacity: int
    used: int
    usable_until: datetime   # naive UTC, like the database's timestamps


class LeaseBook:
    """This process's open leases: at most one in use per campaign."""

    def __init__(self):
        self._leases: dict[str, LocalLease] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def take(self, campaign_id: str, cost: int, now: datetime) -> Optional[str]:
        """Admit a bid against the campaign's lease, if it has room."""
        lease = self._leases.get(campaign_id)
        if lease is None or now >= lease.usable_until or lease.used + cost > lease.capacity:
            return None
        lease.used += cost
        return lease.id

    def put(self, campaign_id: str, lease: LocalLease) -> None:
        self._leases[campaign_id] = lease

    def lock(self, campaign_id: str) -> asyncio.Lock:
        """Serialises taking a new lease, so concurrent bids share one."""
        return self._locks.setdefault(campaign_id, asyncio.Lock())

    def held(self) -> dict[str, LocalLease]:
        return dict(self._leases)

    def reset(self) -> None:
        """Forget every lease, as a restarted process would."""
        self._leases.clear()
        self._locks.clear()


BOOK = LeaseBook()


def lease_size(campaign: Campaign, cost: int, now) -> int:
    """How much to lease for a bid costing at most `cost`, or 0 for none.

    LEASE_FRACTION of the daily budget, but never more than half of what is
    left, nor more than pacing allows yet; and always at least one bid's
    worth, or nothing.
    """
    room = min(
        campaign.remaining_daily_budget_micros,
        campaign.remaining_total_budget_micros,
        allowed_spend_micros(campaign, now)
        - campaign.effective_spent_today_micros - (campaign.reserved_micros or 0),
    )
    if room < cost:
        return 0
    target = int(campaign.daily_budget_micros * LEASE_FRACTION)
    return min(room, max(cost, min(target, room // 2)))


async def acquire(db: AsyncSession, campaign: Campaign, amount: int,
                  now: datetime) -> Optional[LocalLease]:
    """Reserve `amount` on the campaign and record the lease, in one statement.

    The UPDATE matches only while today's spend, everything already reserved
    and this lease together fit both budgets; the INSERT selects from it, so
    a refused reservation records no lease.
    """
    today = now.date()
    spent_today = AuctionEngine._spent_today_expression(today)
    daily_limit = cast(Campaign.daily_budget_cents, BigInteger) * MICROS_PER_CENT
    total_limit = cast(Campaign.total_budget_cents, BigInteger) * MICROS_PER_CENT
    reserved = (
        update(Campaign)
        .where(
            Campaign.id == campaign.id,
            or_(Campaign.status == CampaignStatus.ACTIVE,
                and_(Campaign.status == CampaignStatus.EXHAUSTED,
                     Campaign.spend_date < today)),
            spent_today + Campaign.reserved_micros <= daily_limit - amount,
            Campaign.total_spent_micros + Campaign.reserved_micros <= total_limit - amount,
        )
        .values(reserved_micros=Campaign.reserved_micros + amount)
        .returning(Campaign.id)
    ).cte("reserved")
    lease = LocalLease(id=str(uuid.uuid4()), capacity=amount, used=0,
                       usable_until=now + timedelta(seconds=LEASE_SECONDS))
    row = {
        BudgetLease.id: lease.id, BudgetLease.campaign_id: campaign.id,
        BudgetLease.owner: OWNER, BudgetLease.amount_micros: amount,
        BudgetLease.charged_micros: 0, BudgetLease.status: "open",
        BudgetLease.created_at: now, BudgetLease.usable_until: lease.usable_until,
    }
    recorded = insert(BudgetLease).from_select(
        [c.key for c in row],
        select(*(literal(v, type_=c.type) for c, v in row.items())).select_from(reserved),
    ).cte("recorded")
    taken = (await db.execute(select(reserved.c.id).add_cte(recorded))).one_or_none()
    return lease if taken else None


async def close_finished(db: AsyncSession, now: datetime, bid_ttl_seconds: float,
                         batch: int = 500) -> tuple[int, int]:
    """Close leases whose last bids are past their notice TTL.

    Returns (leases closed, micros given back). Each gives back what its wins
    did not charge. A win notice for one of its bids arriving after this is
    still billed, without a reservation behind it: the one way to overspend,
    which is why the TTL is generous.
    """
    cutoff = now - timedelta(seconds=bid_ttl_seconds)
    leases = (await db.execute(
        select(BudgetLease)
        .where(BudgetLease.status == "open", BudgetLease.usable_until < cutoff)
        .order_by(BudgetLease.usable_until)
        .limit(batch)
        # A win notice may be charging one of these right now: skip it.
        .with_for_update(skip_locked=True)
    )).scalars().all()
    totals: dict[str, int] = {}
    for lease in leases:
        release = lease.amount_micros - lease.charged_micros
        lease.status, lease.closed_at, lease.released_micros = "closed", now, release
        totals[lease.campaign_id] = totals.get(lease.campaign_id, 0) + release
    await db.flush()
    # Campaign rows last and in a fixed order: the lock on each is held only
    # to COMMIT, and two sweepers cannot deadlock.
    for campaign_id in sorted(totals):
        if totals[campaign_id]:
            await db.execute(
                update(Campaign).where(Campaign.id == campaign_id)
                .values(reserved_micros=Campaign.reserved_micros - totals[campaign_id])
                .execution_options(synchronize_session=False))
    return len(leases), sum(totals.values())
