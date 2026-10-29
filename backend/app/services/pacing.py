"""Budget pacing: how much of today's budget a campaign may have used by now.

An asap campaign may use all of it whenever opportunities come. An even
campaign is held to the share of the (UTC) day that has passed, plus a little
headroom so it can take a good impression slightly ahead of schedule rather
than stalling at exactly the line. Without pacing, a campaign that bids high
spends its day in the first hours: the iPinYou replay showed flat bidding do
exactly that.

Reserved spend counts as used: a bid in flight may still be won.
"""
import os
from datetime import datetime, timezone
from typing import Optional

PACING_HEADROOM = float(os.getenv("PACING_HEADROOM", "0.05"))
SECONDS_PER_DAY = 86_400


def day_elapsed(now: Optional[datetime] = None) -> float:
    now = now or datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (now - midnight).total_seconds() / SECONDS_PER_DAY


def allowed_spend_micros(campaign, now: Optional[datetime] = None) -> int:
    """The most of today's budget the campaign may have used at `now`."""
    daily = campaign.daily_budget_micros
    if campaign.pacing != "even":
        return daily
    return min(daily, int(daily * (day_elapsed(now) + PACING_HEADROOM)))


def ahead_of_pace(campaign, charge_micros: int, now: Optional[datetime] = None) -> bool:
    """Whether spending `charge_micros` more now would run ahead of schedule."""
    used = campaign.effective_spent_today_micros + (campaign.reserved_micros or 0)
    return used + charge_micros > allowed_spend_micros(campaign, now)
