from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.database import get_db
from app.models.models import Publisher, AdSlot
from app.schemas.schemas import PublisherCreate, PublisherOut, AdSlotCreate, AdSlotOut

router = APIRouter()


@router.post("/", response_model=PublisherOut, status_code=201)
async def create_publisher(payload: PublisherCreate, db: AsyncSession = Depends(get_db)):
    publisher = Publisher(**payload.model_dump())
    db.add(publisher)
    await db.flush()
    await db.refresh(publisher)
    return publisher


@router.get("/", response_model=list[PublisherOut])
async def list_publishers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Publisher).where(Publisher.is_active == True))
    return result.scalars().all()


@router.post("/slots", response_model=AdSlotOut, status_code=201)
async def create_ad_slot(payload: AdSlotCreate, db: AsyncSession = Depends(get_db)):
    slot = AdSlot(**payload.model_dump())
    db.add(slot)
    await db.flush()
    await db.refresh(slot)
    return slot


@router.get("/slots", response_model=list[AdSlotOut])
async def list_ad_slots(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AdSlot).where(AdSlot.is_active == True))
    return result.scalars().all()


@router.get("/slots/{slot_id}", response_model=AdSlotOut)
async def get_ad_slot(slot_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AdSlot).where(AdSlot.id == slot_id))
    slot = result.scalar_one_or_none()
    if not slot:
        raise HTTPException(status_code=404, detail="Ad slot not found")
    return slot
