import math

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, Float, Integer, cast, desc
from app.core.database import get_db
from app.models.models import AuctionResult, Impression, Click, Campaign
from app.schemas.schemas import AuctionStats, CampaignStats
from app.services.accounting import micros_to_cents

router = APIRouter()


@router.get("/overview", response_model=AuctionStats)
async def get_overview(db: AsyncSession = Depends(get_db)):
    """Aggregate stats for the main dashboard."""

    # Auction metrics
    auction_stats = await db.execute(
        select(
            func.count(AuctionResult.id).label("total"),
            func.avg(AuctionResult.clearing_price_cents)
            .filter(AuctionResult.had_fill == True).label("avg_price"),
            func.avg(AuctionResult.num_bidders).label("avg_bidders"),
            func.sum(
                cast(AuctionResult.had_fill, Integer)
            ).label("filled"),
        )
    )
    row = auction_stats.one()
    total = row.total or 0
    filled = row.filled or 0

    # Impression and click counts
    imp_count = await db.execute(select(func.count(Impression.id)))
    total_impressions = imp_count.scalar() or 0

    click_count = await db.execute(select(func.count(Click.id)))
    total_clicks = click_count.scalar() or 0

    return AuctionStats(
        total_auctions=total,
        fill_rate=round((filled / total * 100) if total > 0 else 0, 2),
        avg_clearing_price_cents=round(row.avg_price or 0, 2),
        avg_bidders_per_auction=round(row.avg_bidders or 0, 2),
        total_impressions=total_impressions,
        total_clicks=total_clicks,
        overall_ctr=round((total_clicks / total_impressions * 100) if total_impressions > 0 else 0, 2),
    )


@router.get("/campaigns", response_model=list[CampaignStats])
async def get_campaign_stats(db: AsyncSession = Depends(get_db)):
    """Per-campaign performance breakdown."""

    campaigns_result = await db.execute(select(Campaign))
    campaigns = campaigns_result.scalars().all()

    # Aggregate once per table rather than three queries per campaign — the
    # dashboard polls this every 5 seconds, so the N+1 version scaled with
    # campaign count and swamped the connection pooler.
    imp_rows = await db.execute(
        select(
            Impression.campaign_id,
            func.count(Impression.id),
            func.avg(Impression.clearing_price_cents),
        ).group_by(Impression.campaign_id)
    )
    imp_by_campaign = {
        cid: (count, avg or 0) for cid, count, avg in imp_rows.all()
    }

    click_rows = await db.execute(
        select(Click.campaign_id, func.count(Click.id)).group_by(Click.campaign_id)
    )
    clicks_by_campaign = dict(click_rows.all())

    stats = []
    for campaign in campaigns:
        impressions, avg_cpm_val = imp_by_campaign.get(campaign.id, (0, 0))
        clicks = clicks_by_campaign.get(campaign.id, 0)

        stats.append(CampaignStats(
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            impressions=impressions,
            clicks=clicks,
            ctr=round((clicks / impressions * 100) if impressions > 0 else 0, 2),
            total_spend_cents=campaign.total_spent_cents,
            avg_cpm_cents=round(avg_cpm_val, 2),
        ))

    return stats


@router.get("/timeseries")
async def get_timeseries(db: AsyncSession = Depends(get_db)):
    """
    Auctions per minute for the live chart.

    Takes the 60 most recent minutes (order desc, then limit) and flips them
    back to chronological order for plotting. Ordering ascending before the
    limit returned the *oldest* 60 minutes, which froze the "live" chart on
    the first hour of data forever.
    """
    minute = func.date_trunc("minute", AuctionResult.created_at).label("minute")
    result = await db.execute(
        select(
            minute,
            func.count(AuctionResult.id).label("auctions"),
            func.sum(cast(AuctionResult.had_fill, Integer)).label("fills"),
            func.avg(AuctionResult.clearing_price_cents).label("avg_price"),
        )
        .group_by(minute)
        .order_by(desc(minute))
        .limit(60)
    )
    rows = list(reversed(result.all()))
    return [
        {
            "minute": row.minute.isoformat() if row.minute else None,
            "auctions": row.auctions,
            "fills": row.fills or 0,
            "avg_price_cents": round(row.avg_price or 0, 2),
        }
        for row in rows
    ]


def build_ab_comparison(rows) -> list[dict]:
    """Shape raw per-strategy aggregates into A/B metrics for both arms.

    rows: iterable of (strategy, impressions, spend_micros, clicks,
          sum_spend_sq, sum_clicks_sq, sum_spend_clicks), the last three over
          impressions, for the variance of the CPC (see cpc_comparison).

    The headline metric is effective CPC (spend / clicks): EV bidding should
    pay less per click by not overpaying on low-pCTR impressions. CTR is
    reported as context but is near-equal across arms by construction — click
    probability depends on the auction context, which is assigned
    independently of the arm.
    """
    by_strategy = {r[0]: r[1:] for r in rows}
    out = []
    for strategy in ("control", "treatment"):
        n, spend_micros, clicks, ss, kk, sk = by_strategy.get(
            strategy, (0, 0, 0, 0, 0, 0))
        n, clicks = int(n or 0), int(clicks or 0)
        spend_micros = int(spend_micros or 0)
        spend_cents = micros_to_cents(spend_micros)
        out.append({
            "strategy": strategy,
            "impressions": n,
            "clicks": clicks,
            "spend_cents": spend_cents,
            "ctr": (clicks / n) if n else 0.0,
            "eff_cpc_cents": (spend_cents / clicks) if clicks else None,
            "clicks_per_1000_cents": (
                clicks * 1000 / spend_cents if spend_cents else 0.0
            ),
            "cpc_log_variance": _log_cpc_variance(
                n, spend_micros, clicks, float(ss or 0), float(kk or 0),
                float(sk or 0)),
        })
    return out


def _log_cpc_variance(n, spend, clicks, ss, kk, sk) -> float | None:
    """Variance of log(CPC) for one arm, by the delta method.

    CPC = sum(spend) / sum(clicks) is a ratio of two per-impression means, and
    impressions, not clicks, are the randomised unit, so neither the spend nor
    the click count can be treated as fixed. The delta method linearises the
    ratio: Var(R) ~ (Var(s) - 2R Cov(s, k) + R^2 Var(k)) / (n * mean(k)^2).
    """
    if n < 2 or clicks == 0:
        return None
    mean_s, mean_k = spend / n, clicks / n
    var_s = ss / n - mean_s ** 2
    var_k = kk / n - mean_k ** 2
    cov = sk / n - mean_s * mean_k
    ratio = mean_s / mean_k
    if ratio == 0:
        return None
    var_ratio = (var_s - 2 * ratio * cov + ratio ** 2 * var_k) / (n * mean_k ** 2)
    return max(var_ratio, 0.0) / ratio ** 2


def cpc_comparison(arms: list[dict], z: float = 1.96) -> dict:
    """Treatment's change in CPC relative to control, with a 95% interval.

    The interval is taken on log(CPC_t / CPC_c), whose variance is the sum of
    the two arms', then mapped back to a percentage change. The verdict says
    whether that interval excludes zero: an observed difference alone is not
    a result, and on a few dozen clicks it is almost always noise.
    """
    by_strategy = {a["strategy"]: a for a in arms}
    control, treatment = by_strategy["control"], by_strategy["treatment"]
    clicks = {"control_clicks": control["clicks"],
              "treatment_clicks": treatment["clicks"]}
    var_c, var_t = control["cpc_log_variance"], treatment["cpc_log_variance"]
    if var_c is None or var_t is None:
        return {"verdict": "insufficient_data", "change": None,
                "ci_low": None, "ci_high": None, **clicks}

    log_ratio = math.log(treatment["eff_cpc_cents"] / control["eff_cpc_cents"])
    half_width = z * math.sqrt(var_c + var_t)
    low, high = math.exp(log_ratio - half_width) - 1, math.exp(log_ratio + half_width) - 1
    if high < 0:
        verdict = "treatment_cheaper"
    elif low > 0:
        verdict = "treatment_dearer"
    else:
        verdict = "inconclusive"
    return {"verdict": verdict, "change": math.exp(log_ratio) - 1,
            "ci_low": low, "ci_high": high, **clicks}


@router.get("/ab-comparison")
async def ab_comparison(db: AsyncSession = Depends(get_db)):
    """Per-arm cost efficiency for the bidding A/B, and whether the difference
    between the arms is statistically distinguishable from noise."""
    clicks_per_impression = (
        select(Click.impression_id, func.count().label("clicks"))
        .group_by(Click.impression_id)
        .subquery()
    )
    spend = cast(Impression.charged_cost_micros, Float)
    clicks = cast(func.coalesce(clicks_per_impression.c.clicks, 0), Float)
    result = await db.execute(
        select(
            AuctionResult.strategy,
            func.count(Impression.id),
            func.sum(Impression.charged_cost_micros),
            func.sum(clicks),
            func.sum(spend * spend),
            func.sum(clicks * clicks),
            func.sum(spend * clicks),
        )
        .join(Impression, Impression.auction_id == AuctionResult.auction_id)
        .outerjoin(clicks_per_impression,
                   clicks_per_impression.c.impression_id == Impression.id)
        # House auctions only. Exchange impressions get real clicks, the
        # house's are simulated, and mixing outcome sources in one arm
        # would dilute it exactly the way unclicked visitor auctions did.
        .where(AuctionResult.source == "house")
        .group_by(AuctionResult.strategy)
    )
    arms = build_ab_comparison(result.all())
    return {"arms": arms, "comparison": cpc_comparison(arms)}
