from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.database import engine, ensure_schema
from app.core.config import get_settings

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs on startup: create all tables if they don't exist.
    In production you'd use Alembic migrations instead.

    A database that is unreachable at boot must not take the whole app down —
    otherwise /health can't report the problem and the dashboard has nothing
    to show but a blank page.
    """
    # Plain ASCII on purpose: a Windows console defaults to cp1252 and raises
    # UnicodeEncodeError on emoji, which is enough to abort startup entirely.
    try:
        await ensure_schema()
        print("[startup] Database tables ready")
    except Exception as e:
        print(f"[startup] Schema init failed (continuing so /health stays up): {e}")
    yield
    # Shutdown: dispose connection pool
    await engine.dispose()
    print("[shutdown] Database connections closed")


app = FastAPI(
    title="RTB Auction Engine",
    description="Real-Time Bidding auction engine — programmatic advertising infrastructure",
    version="1.0.0",
    lifespan=lifespan,
)

# Public read-only demo — no cookies or auth headers are ever sent, so
# wildcard origins with credentials disabled is both valid and sufficient.
# ("*" with allow_credentials=True is rejected by browsers.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
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


@app.get("/")
async def root():
    """Anyone who opens the API URL directly should land somewhere useful."""
    return {
        "service": "RTB Auction Engine",
        "docs": "/docs",
        "health": "/health",
        "api_base": "/api/v1",
    }


@app.get("/health")
async def health():
    """Reports database reachability so a broken deploy is diagnosable."""
    db_ok = True
    db_error = None
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        db_ok = False
        db_error = str(e)[:200]

    return {
        "status": "ok" if db_ok else "degraded",
        "version": "1.0.0",
        "environment": settings.ENVIRONMENT,
        "database": "connected" if db_ok else "unreachable",
        "database_error": db_error,
    }
