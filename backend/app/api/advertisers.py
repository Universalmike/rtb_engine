from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.database import get_db
from app.models.models import Advertiser
from app.schemas.schemas import AdvertiserCreate, AdvertiserOut

router = APIRouter()


@router.post("/", response_model=AdvertiserOut, status_code=201)
async def create_advertiser(payload: AdvertiserCreate, db: AsyncSession = Depends(get_db)):
    advertiser = Advertiser(**payload.model_dump())
    db.add(advertiser)
    await db.flush()
    await db.refresh(advertiser)
    return advertiser


@router.get("/", response_model=list[AdvertiserOut])
async def list_advertisers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Advertiser).where(Advertiser.is_active == True))
    return result.scalars().all()


@router.get("/{advertiser_id}", response_model=AdvertiserOut)
async def get_advertiser(advertiser_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Advertiser).where(Advertiser.id == advertiser_id))
    advertiser = result.scalar_one_or_none()
    if not advertiser:
        raise HTTPException(status_code=404, detail="Advertiser not found")
    return advertiser
