import os

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
