from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from app.core.database import get_db
from app.models.models import AuctionResult, Impression, Click
from app.schemas.schemas import BidRequest, BidResponse, RecentAuction
from app.services.auction_engine import AuctionEngine

router = APIRouter()


@router.post("/bid", response_model=BidResponse)
async def submit_bid_request(
    bid_request: BidRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Core RTB endpoint. A publisher fires this when a user loads a page.

    The engine runs a full auction and returns the winner. Warm, that measures
    ~150ms on the deployed free tier: four database round trips and roughly
    0.1ms of compute. Set AUCTION_PROFILE=1 for the per-phase breakdown.
    """
    engine = AuctionEngine(db)
    return await engine.run_auction(bid_request)


@router.post("/impression/{auction_id}/click")
async def record_click(auction_id: str, db: AsyncSession = Depends(get_db)):
    """
    Called when a user clicks the winning ad.
    Looks up the impression by auction_id and creates a click record.
    """
    result = await db.execute(
        select(Impression).where(Impression.auction_id == auction_id)
    )
    impression = result.scalar_one_or_none()
    if not impression:
        raise HTTPException(status_code=404, detail="Impression not found")

    click = Click(
        impression_id=impression.id,
        campaign_id=impression.campaign_id,
    )
    db.add(click)
    await db.flush()
    return {"message": "Click recorded", "auction_id": auction_id}


@router.get("/recent", response_model=list[RecentAuction])
async def recent_auctions(limit: int = 20, db: AsyncSession = Depends(get_db)):
    """Returns the most recent auction results for the dashboard."""
    result = await db.execute(
        select(AuctionResult)
        .order_by(desc(AuctionResult.created_at))
        .limit(limit)
    )
    return result.scalars().all()
