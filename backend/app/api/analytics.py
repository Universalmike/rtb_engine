from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, Integer, cast, desc
from app.core.database import get_db
from app.models.models import AuctionResult, Impression, Click, Campaign
from app.schemas.schemas import AuctionStats, CampaignStats

router = APIRouter()


@router.get("/overview", response_model=AuctionStats)
async def get_overview(db: AsyncSession = Depends(get_db)):
    """Aggregate stats for the main dashboard."""

    # Auction metrics
    auction_stats = await db.execute(
        select(
            func.count(AuctionResult.id).label("total"),
            func.avg(AuctionResult.clearing_price_cents).label("avg_price"),
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