from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://rtb:rtb_secret@localhost:5432/rtb_db"
    REDIS_URL: str = "redis://localhost:6379"
    ENVIRONMENT: str = "development"
    SECRET_KEY: str = "dev-secret-key"

    # Auction settings
    BID_TIMEOUT_MS: int = 100
    MIN_BID_FLOOR: float = 0.01
    AUCTION_FEE_PCT: float = 0.15

    class Config:
        env_file = ".env"

    @property
    def async_database_url(self) -> str:
        """
        Supabase and Render both provide standard postgres:// URLs.
        SQLAlchemy async requires postgresql+asyncpg://.
        Also disables prepared statements — required for Supabase's pgBouncer pooler.
        """
        url = self.DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


@lru_cache()
def get_settings() -> Settings:
    return Settings()
