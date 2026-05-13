"""Redis-backed single active session enforcement.

Only one session per account is allowed at any time.

Redis keys used:
    luck:active_session:<user_id>   -> session nonce (TTL = token lifetime)
    luck:conflict:<conflict_token>  -> {user_id, role} (TTL = 5 min)
"""
import json
import secrets

from core.redis_client import get_redis, key

_SESSION_TTL = 8 * 3600   # 8 hours — matches the signed token lifetime
_CONFLICT_TTL = 5 * 60    # 5 minutes for the force-login confirmation window


# ---------------------------------------------------------------------------
# Active session management
# ---------------------------------------------------------------------------

async def get_active_nonce(user_id: str) -> str | None:
    """Return the stored session nonce for user_id, or None if no session."""
    return await get_redis().get(key("active_session", user_id))


async def set_active_session(user_id: str, nonce: str) -> None:
    """Register nonce as the single active session for user_id."""
    await get_redis().setex(key("active_session", user_id), _SESSION_TTL, nonce)


async def invalidate_session(user_id: str) -> None:
    """Terminate the active session for user_id (all devices/tabs)."""
    await get_redis().delete(key("active_session", user_id))


async def replace_session(user_id: str, new_nonce: str) -> None:
    """Atomically replace any existing session with a new nonce."""
    r = get_redis()
    pipe = r.pipeline()
    pipe.set(key("active_session", user_id), new_nonce, ex=_SESSION_TTL)
    await pipe.execute()


async def is_session_valid(user_id: str, nonce: str) -> bool:
    """Return True only if nonce matches the currently stored active nonce."""
    stored = await get_active_nonce(user_id)
    return stored is not None and stored == nonce


# ---------------------------------------------------------------------------
# Conflict / force-login token
# ---------------------------------------------------------------------------

async def create_conflict_token(user_id: str, role: str) -> str:
    """
    Store a short-lived conflict token after a second-login attempt.
    Returns the opaque token to embed in the confirmation redirect.
    """
    token = secrets.token_urlsafe(32)
    await get_redis().setex(
        key("conflict", token),
        _CONFLICT_TTL,
        json.dumps({"user_id": user_id, "role": role}),
    )
    return token


async def consume_conflict_token(token: str) -> dict | None:
    """
    Consume and return {user_id, role} for a valid conflict token.
    Returns None if the token is expired or never existed.
    One-time use — deleted on read.
    """
    raw = await get_redis().getdel(key("conflict", token))
    if raw is None:
        return None
    return json.loads(raw)
