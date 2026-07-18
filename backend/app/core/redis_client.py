import redis.asyncio as aioredis
from app.core.config import get_settings

settings = get_settings()

# Single connection pool shared across all requests
redis_pool = aioredis.ConnectionPool.from_url(
    settings.REDIS_URL,
    max_connections=20,
    decode_responses=True,   # Always return strings, not bytes
)


def get_redis() -> aioredis.Redis:
    """Returns a Redis client using the shared pool."""
    return aioredis.Redis(connection_pool=redis_pool)


# Stream names — centralised so nothing is hardcoded in two places
IMPRESSION_STREAM = "rtb:impressions"
CLICK_STREAM = "rtb:clicks"
BID_STREAM = "rtb:bids"
