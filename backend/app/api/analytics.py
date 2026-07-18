from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, Integer, cast
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

    stats = []
    for campaign in campaigns:
        imp_result = await db.execute(
            select(func.count(Impression.id))
            .where(Impression.campaign_id == campaign.id)
        )
        impressions = imp_result.scalar() or 0

        click_result = await db.execute(
            select(func.count(Click.id))
            .where(Click.campaign_id == campaign.id)
        )
        clicks = click_result.scalar() or 0

        avg_cpm = await db.execute(
            select(func.avg(Impression.clearing_price_cents))
            .where(Impression.campaign_id == campaign.id)
        )
        avg_cpm_val = avg_cpm.scalar() or 0

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
    """Auctions per minute for the live chart."""
    result = await db.execute(
        select(
            func.date_trunc("minute", AuctionResult.created_at).label("minute"),
            func.count(AuctionResult.id).label("auctions"),
            func.sum(cast(AuctionResult.had_fill, Integer)).label("fills"),
            func.avg(AuctionResult.clearing_price_cents).label("avg_price"),
        )
        .group_by("minute")
        .order_by("minute")
        .limit(60)
    )
    rows = result.all()
    return [
        {
            "minute": row.minute.isoformat() if row.minute else None,
            "auctions": row.auctions,
            "fills": row.fills or 0,
            "avg_price_cents": round(row.avg_price or 0, 2),
        }
        for row in rows
    ]