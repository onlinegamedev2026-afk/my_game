"""Central Redis client module.

All callers should use `get_redis()` to obtain the shared client. The client
is initialised once at application startup via `init_redis()`.

Key-prefix convention:
    luck:captcha:<token>
    luck:otp:<token>
    luck:child_otp:<token>
    luck:admin_pwd_otp:<token>
    luck:otp_rate:<actor_id>:<email>:<ip>
    luck:session:<user_id>          ← active session token (single active session)
    luck:game:<game_key>:phase
    luck:game:<game_key>:session_id
    luck:game:<game_key>:phase_ends_at
    luck:game:<game_key>:cards_dealt
    luck:game:<game_key>:winner
    luck:game:<game_key>:joker
    luck:game:<game_key>:winning_card
    luck:lock:game:<game_key>
"""
import logging

import redis.asyncio as aioredis
import redis as syncredis

from core.config import settings

log = logging.getLogger(__name__)

_async_client: aioredis.Redis | None = None
_sync_client: syncredis.Redis | None = None

PREFIX = "luck:"


def key(*parts: str) -> str:
    return PREFIX + ":".join(parts)


def session_key(user_id: str) -> str:
    """Redis key that stores the single active session token for a user."""
    return key("session", user_id)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def init_redis() -> None:
    global _async_client, _sync_client
    _async_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    _sync_client = syncredis.from_url(settings.redis_url, decode_responses=True)
    log.info("Redis client initialised: %s", settings.redis_url)


async def close_redis() -> None:
    global _async_client
    if _async_client:
        await _async_client.aclose()
        _async_client = None
        log.info("Redis async client closed.")


def get_redis() -> aioredis.Redis:
    if _async_client is None:
        raise RuntimeError("Redis not initialised — call init_redis() first.")
    return _async_client


def get_sync_redis() -> syncredis.Redis:
    if _sync_client is None:
        raise RuntimeError("Redis not initialised — call init_redis() first.")
    return _sync_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class DistributedLock:
    """Simple Redis-based distributed lock using SET NX PX."""

    def __init__(self, name: str, ttl_ms: int = 60_000) -> None:
        self._key = key("lock", name)
        self._ttl_ms = ttl_ms

    async def acquire(self, value: str = "1") -> bool:
        r = get_redis()
        return bool(await r.set(self._key, value, nx=True, px=self._ttl_ms))

    async def release(self) -> None:
        r = get_redis()
        await r.delete(self._key)

    async def refresh(self) -> None:
        r = get_redis()
        await r.pexpire(self._key, self._ttl_ms)
