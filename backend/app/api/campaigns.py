from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import json
from app.core.database import get_db
from app.models.models import Campaign, CampaignStatus
from app.schemas.schemas import CampaignCreate, CampaignOut

router = APIRouter()


@router.post("/", response_model=CampaignOut, status_code=201)
async def create_campaign(payload: CampaignCreate, db: AsyncSession = Depends(get_db)):
    data = payload.model_dump()
    # Serialize list fields to JSON strings for storage
    data["target_countries"] = json.dumps(data["target_countries"])
    data["target_devices"] = json.dumps(data["target_devices"])
    data["target_categories"] = json.dumps(data["target_categories"])
    campaign = Campaign(**data)
    db.add(campaign)
    await db.flush()
    await db.refresh(campaign)
    return campaign


@router.get("/", response_model=list[CampaignOut])
async def list_campaigns(
    status: CampaignStatus = None,
    db: AsyncSession = Depends(get_db)
):
    query = select(Campaign)
    if status:
        query = query.where(Campaign.status == status)
    result = await db.execute(query.order_by(Campaign.created_at.desc()))
    return result.scalars().all()


@router.get("/{campaign_id}", response_model=CampaignOut)
async def get_campaign(campaign_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


@router.patch("/{campaign_id}/pause")
async def pause_campaign(campaign_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    campaign.status = CampaignStatus.PAUSED
    await db.flush()
    return {"message": "Campaign paused", "campaign_id": campaign_id}


@router.patch("/{campaign_id}/activate")
async def activate_campaign(campaign_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    campaign.status = CampaignStatus.ACTIVE
    campaign.spent_today_cents = 0  # Reset daily spend on re-activation
    await db.flush()
    return {"message": "Campaign activated", "campaign_id": campaign_id}
