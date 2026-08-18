import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool
from app.core.config import get_settings

settings = get_settings()

# On serverless (Vercel), every invocation may run in its own short-lived
# container. A per-container connection pool multiplies out fast and exhausts
# the database's connection limit, so we pool nothing and let the connection
# pooler (pgBouncer/Supabase) do that job instead.
IS_SERVERLESS = bool(os.getenv("VERCEL"))

pool_args = (
    {"poolclass": NullPool}
    if IS_SERVERLESS
    else {"pool_size": 5, "max_overflow": 5, "pool_pre_ping": True}
)

# pgBouncer in transaction mode can't use prepared statements. Supabase's
# pooler and Neon both need this; harmless on a direct connection.
connect_args = {}
if "supabase" in settings.DATABASE_URL or "pooler" in settings.DATABASE_URL:
    connect_args = {"statement_cache_size": 0, "prepared_statement_cache_size": 0}

engine = create_async_engine(
    settings.async_database_url,
    echo=settings.ENVIRONMENT == "development",
    connect_args=connect_args,
    **pool_args,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def ensure_schema() -> None:
    """
    Create tables if they don't exist.

    Called from the app lifespan locally, and again from the seed endpoint
    because Vercel's ASGI adapter does not reliably run lifespan events —
    without this the demo can boot against an empty database.
    In production you'd run Alembic migrations instead.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def warm_connection_pool() -> None:
    """Establish every pooled connection at boot rather than on first use.

    Opening a connection to a hosted Postgres costs a TCP and TLS handshake.
    Left lazy, that cost is paid by whoever first loads the dashboard — and
    the dashboard opens seven requests at once, so it is paid several times
    over, *inside* the auction the visitor is waiting on. Free-tier hosts
    spin containers down when idle, which makes that first visitor the common
    case rather than a rare one.

    Connections are opened concurrently and held simultaneously, otherwise
    each would simply check the same pooled connection back out. Failures are
    not fatal: this is an optimisation, and /health reports real outages.
    """
    if IS_SERVERLESS:
        return 0  # NullPool holds nothing between requests; nothing to warm

    size = engine.pool.size()
    connections = await asyncio.gather(
        *(engine.connect() for _ in range(size)), return_exceptions=True
    )
    live = [c for c in connections if not isinstance(c, BaseException)]
    try:
        await asyncio.gather(
            *(c.execute(text("SELECT 1")) for c in live), return_exceptions=True
        )
    finally:
        await asyncio.gather(
            *(c.close() for c in live), return_exceptions=True
        )
    return len(live)


async def reset_schema() -> None:
    """Drop and recreate every table. Used only by the seed endpoint — the demo
    data is disposable, and this lets schema changes take effect without Alembic."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
