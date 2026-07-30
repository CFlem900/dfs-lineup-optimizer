import fnmatch
import json
import logging
import time
from typing import Optional, Any

from app.config import get_settings

logger = logging.getLogger(__name__)


class CacheService:
    """Redis-backed caching service with fallback to in-memory dict."""

    def __init__(self):
        self._settings = get_settings()
        self._redis = None
        # Fallback stores (value, expiry_timestamp | None)
        self._fallback: dict[str, tuple[Any, float | None]] = {}
        self._connected = False

    async def connect(self):
        """Initialize Redis connection."""
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.Redis(
                host=self._settings.redis_host,
                port=self._settings.redis_port,
                db=self._settings.redis_db,
                password=self._settings.redis_password or None,
                decode_responses=True,
            )
            await self._redis.ping()
            self._connected = True
            logger.info("Redis connected successfully")
        except Exception as e:
            logger.warning(f"Redis unavailable, using in-memory cache: {e}")
            self._connected = False

    async def disconnect(self):
        """Close Redis connection."""
        if self._redis and self._connected:
            await self._redis.close()
            self._connected = False
            logger.info("Redis disconnected")

    async def get(self, key: str) -> Optional[Any]:
        """Retrieve a value from cache."""
        try:
            if self._connected and self._redis:
                value = await self._redis.get(key)
                if value:
                    return json.loads(value)
            else:
                entry = self._fallback.get(key)
                if entry is not None:
                    value, expires_at = entry
                    if expires_at is not None and time.time() > expires_at:
                        # Expired — evict lazily
                        del self._fallback[key]
                        return None
                    return value
        except Exception as e:
            logger.error(f"Cache GET error for '{key}': {e}")
        return None

    async def set(self, key: str, value: Any, ttl: Optional[int] = None):
        """Store a value in cache with optional TTL."""
        try:
            serialized = json.dumps(value, default=str)
            if self._connected and self._redis:
                if ttl:
                    await self._redis.setex(key, ttl, serialized)
                else:
                    await self._redis.set(key, serialized)
            else:
                expires_at = (time.time() + ttl) if ttl else None
                self._fallback[key] = (value, expires_at)
        except Exception as e:
            logger.error(f"Cache SET error for '{key}': {e}")

    async def delete(self, key: str):
        """Remove a value from cache."""
        try:
            if self._connected and self._redis:
                await self._redis.delete(key)
            else:
                self._fallback.pop(key, None)  # pops the (value, expires) tuple
        except Exception as e:
            logger.error(f"Cache DELETE error for '{key}': {e}")

    async def clear_pattern(self, pattern: str):
        """Delete all keys matching a pattern."""
        try:
            if self._connected and self._redis:
                cursor = 0
                while True:
                    cursor, keys = await self._redis.scan(
                        cursor=cursor, match=pattern, count=100
                    )
                    if keys:
                        await self._redis.delete(*keys)
                    if cursor == 0:
                        break
            else:
                keys_to_remove = [
                    k for k in self._fallback if fnmatch.fnmatch(k, pattern)
                ]
                for k in keys_to_remove:
                    del self._fallback[k]
        except Exception as e:
            logger.error(f"Cache CLEAR error for pattern '{pattern}': {e}")

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_baseline_ttl(self) -> int:
        return self._settings.cache_ttl_baseline

    def get_rotation_ttl(self) -> int:
        return self._settings.cache_ttl_rotation

    def get_injuries_ttl(self) -> int:
        return self._settings.cache_ttl_injuries
