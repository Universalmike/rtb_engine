from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.database import engine, Base
from app.core.config import get_settings

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs on startup: create all tables if they don't exist.
    In production you'd use Alembic migrations instead.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✅ Database tables created")
    yield
    # Shutdown: dispose connection pool
    await engine.dispose()
    print("🔌 Database connections closed")


app = FastAPI(
    title="RTB Auction Engine",
    description="Real-Time Bidding auction engine — programmatic advertising infrastructure",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Routers (added as we build each day) ────────────────────────────────────
from app.api import advertisers, campaigns, publishers, auction, analytics, seed

app.include_router(advertisers.router, prefix="/api/v1/advertisers", tags=["Advertisers"])
app.include_router(campaigns.router, prefix="/api/v1/campaigns", tags=["Campaigns"])
app.include_router(publishers.router, prefix="/api/v1/publishers", tags=["Publishers"])
app.include_router(auction.router, prefix="/api/v1/auction", tags=["Auction"])
app.include_router(analytics.router, prefix="/api/v1/analytics", tags=["Analytics"])
app.include_router(seed.router, prefix="/api/v1/seed", tags=["Seed"])


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0", "environment": settings.ENVIRONMENT}
