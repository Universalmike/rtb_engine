import asyncio
import os
from urllib.parse import urlsplit

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

def uses_transaction_pooler(database_url: str) -> bool:
    """Whether prepared statements must not be cached against this endpoint.

    asyncpg caches prepared statements per connection. That only pays off if a
    statement survives from one checkout to the next, which requires the same
    backend connection each time.

    Measured against Supabase's session pooler (port 5432), it does not: with
    caching enabled the auction ran 2.2x slower (104ms -> 230ms), and the
    first query of each request cost 4x more (35ms -> 142ms) with no warm-up
    at all between calls -- the statement was being re-prepared every request.
    Session mode is documented as prepared-statement safe, but the pooler
    evidently does not hand back the same backend connection, so the cache
    never hits and only the PREPARE round trip remains.

    So the rule is any pgBouncer-fronted endpoint, not just transaction mode:

      db.<ref>.supabase.co:5432                direct       may cache
      <region>.pooler.supabase.com:5432        session      must not cache
      <region>.pooler.supabase.com:6543        transaction  must not cache
      ep-<id>-pooler.<region>.aws.neon.tech    Neon pooled  must not cache

    An unparseable URL returns True: disabling the cache costs performance,
    while wrongly enabling it is both slower and, on transaction mode, an
    outright runtime error.
    """
    try:
        parts = urlsplit(database_url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return True

    if "pooler." in host:   # Supabase (either mode) and Neon's pooled endpoint
        return True
    if port == 6543:        # transaction pooler on an unrecognised host
        return True
    return False


# Escape hatch: if an endpoint is misdetected in production, flip this without
# a code change. Unset means the URL decides.
_cache_override = os.getenv("DB_DISABLE_STATEMENT_CACHE")
DISABLE_STATEMENT_CACHE = (
    _cache_override.lower() in {"1", "true", "yes"}
    if _cache_override is not None
    else uses_transaction_pooler(settings.DATABASE_URL)
)

connect_args = (
    {"statement_cache_size": 0, "prepared_statement_cache_size": 0}
    if DISABLE_STATEMENT_CACHE
    else {}
)

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
