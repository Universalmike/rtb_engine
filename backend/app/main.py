import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.database import (
    AsyncSessionLocal, DISABLE_STATEMENT_CACHE, engine, ensure_schema,
    warm_connection_pool,
)
from app.core.config import get_settings
from app.ml import ipinyou

settings = get_settings()

SWEEP_INTERVAL_SECONDS = float(os.getenv("SWEEP_INTERVAL_SECONDS", "15"))


async def sweep_expired_bids():
    """Release reservations of exchange bids that never got a notice.

    Most exchanges never send loss notices, so without this every lost bid
    would hold its campaign's budget forever.
    """
    from app.services.openrtb import Bidder
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            async with AsyncSessionLocal() as session:
                released = await Bidder(session).expire_stale()
                await session.commit()
            if released:
                print(f"[sweeper] released {released} expired exchange bids")
        except Exception as e:
            print(f"[sweeper] failed, will retry: {e}")


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

    # Pay the connection handshakes here, where nobody is waiting on them,
    # instead of inside the first visitor's auction.
    # Which endpoint mode was detected decides whether Postgres can reuse
    # query plans, so make it visible rather than something to infer.
    print(
        "[startup] Prepared statement cache: "
        + ("disabled (transaction pooler)" if DISABLE_STATEMENT_CACHE
           else "enabled (direct or session endpoint)")
    )
    try:
        opened = await warm_connection_pool()
        print(f"[startup] Connection pool warm ({opened} connections)")
    except Exception as e:
        print(f"[startup] Pool warm-up skipped: {e}")

    # The replay sample takes about half a second to parse; not in a request.
    model, sample = ipinyou.load_model(), ipinyou.load_sample()
    print(f"[startup] iPinYou replay: "
          + (f"model {model['version']}, {len(sample)} requests" if model and sample
             else "artifacts missing, /api/v1/replay disabled"))

    sweeper = asyncio.create_task(sweep_expired_bids())
    yield
    sweeper.cancel()
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
from app.api import (
    advertisers, campaigns, publishers, auction, analytics, seed, replay, openrtb,
)

app.include_router(advertisers.router, prefix="/api/v1/advertisers", tags=["Advertisers"])
app.include_router(campaigns.router, prefix="/api/v1/campaigns", tags=["Campaigns"])
app.include_router(publishers.router, prefix="/api/v1/publishers", tags=["Publishers"])
app.include_router(auction.router, prefix="/api/v1/auction", tags=["Auction"])
app.include_router(analytics.router, prefix="/api/v1/analytics", tags=["Analytics"])
app.include_router(seed.router, prefix="/api/v1/seed", tags=["Seed"])
app.include_router(replay.router, prefix="/api/v1/replay", tags=["Replay"])
app.include_router(openrtb.router, prefix="/api/v1/openrtb", tags=["OpenRTB"])


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
