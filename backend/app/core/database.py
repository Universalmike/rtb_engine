from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.core.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.async_database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    echo=settings.ENVIRONMENT == "development",
    # Supabase uses pgBouncer in transaction mode.
    # Prepared statements don't work with pgBouncer — disable them.
    connect_args={"statement_cache_size": 0} if "supabase" in settings.DATABASE_URL else {},
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


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
