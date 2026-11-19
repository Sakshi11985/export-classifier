from __future__ import annotations

import redis
import redis.asyncio as aredis

from .config import Settings


def sync_redis(s: Settings) -> redis.Redis | None:
    if not s.redis_url:
        return None
    return redis.Redis.from_url(s.redis_url, decode_responses=True, socket_timeout=3, socket_connect_timeout=3)


def async_redis(s: Settings) -> aredis.Redis:
    if not s.redis_url:
        raise RuntimeError("REDIS_URL is not set (see .env.example)")
    return aredis.Redis.from_url(s.redis_url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
