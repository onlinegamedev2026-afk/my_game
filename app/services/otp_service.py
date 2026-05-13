"""Redis-backed OTP and rate-limiting service.

Replaces all in-process OTP_STORE / CHILD_EMAIL_OTP_STORE / ADMIN_PWD_OTP_STORE
/ OTP_RATE dicts. Each OTP record lives in Redis with a TTL.
"""
import hashlib
import json
import secrets
import time

from core.config import settings
from core.redis_client import get_redis, key

OTP_TTL = 30 * 60          # 30 minutes
COOLDOWN = 60               # seconds between sends
WINDOW = 30 * 60            # rate-limit window
MAX_PER_WINDOW = 5


def _otp_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Login OTP (ADMIN / AGENT two-factor)
# ---------------------------------------------------------------------------

async def create_login_otp(actor_id: str, actor_role: str) -> tuple[str, str]:
    """Returns (otp_token, otp_code)."""
    code = f"{secrets.randbelow(900000) + 100000}"
    token = secrets.token_urlsafe(32)
    r = get_redis()
    await r.setex(
        key("otp", token),
        OTP_TTL,
        json.dumps({"actor_id": actor_id, "role": actor_role, "code_hash": _otp_hash(code)}),
    )
    return token, code


async def verify_login_otp(token: str, code: str) -> dict | None:
    """Returns the OTP payload dict if valid, else None. Consumes the token."""
    r = get_redis()
    rk = key("otp", token)
    raw = await r.getdel(rk)
    if raw is None:
        return None
    payload = json.loads(raw)
    if not secrets.compare_digest(payload["code_hash"], _otp_hash(code.strip())):
        return None
    return payload


# ---------------------------------------------------------------------------
# Child email OTP (verifying agent email before account creation)
# ---------------------------------------------------------------------------

async def create_child_email_otp(creator_id: str, email: str) -> tuple[str, str]:
    code = f"{secrets.randbelow(900000) + 100000}"
    token = secrets.token_urlsafe(32)
    r = get_redis()
    await r.setex(
        key("child_otp", token),
        OTP_TTL,
        json.dumps({"creator_id": creator_id, "email": email, "code_hash": _otp_hash(code), "verified": False}),
    )
    return token, code


async def verify_child_email_otp(token: str, email: str, code: str, creator_id: str) -> bool:
    r = get_redis()
    rk = key("child_otp", token)
    raw = await r.get(rk)
    if raw is None:
        return False
    payload = json.loads(raw)
    if payload["creator_id"] != creator_id or payload["email"] != email.strip():
        return False
    if not secrets.compare_digest(payload["code_hash"], _otp_hash(code.strip())):
        return False
    payload["verified"] = True
    await r.setex(rk, OTP_TTL, json.dumps(payload))
    return True


async def consume_child_email_otp(token: str) -> None:
    r = get_redis()
    await r.delete(key("child_otp", token))


async def require_verified_child_email(actor_id: str, email: str, token: str) -> None:
    """Raise ValueError if the OTP record is missing or not verified."""
    r = get_redis()
    raw = await r.get(key("child_otp", token))
    if raw is None:
        raise ValueError("Verify the agent email with OTP before generating credentials.")
    payload = json.loads(raw)
    if (
        not payload.get("verified")
        or payload["creator_id"] != actor_id
        or payload["email"] != email.strip()
    ):
        raise ValueError("Verify the agent email with OTP before generating credentials.")


# ---------------------------------------------------------------------------
# Admin password-change OTP
# ---------------------------------------------------------------------------

async def create_admin_pwd_otp(actor_id: str) -> tuple[str, str]:
    code = f"{secrets.randbelow(900000) + 100000}"
    token = secrets.token_urlsafe(32)
    r = get_redis()
    await r.setex(
        key("admin_pwd_otp", token),
        OTP_TTL,
        json.dumps({"actor_id": actor_id, "code_hash": _otp_hash(code)}),
    )
    return token, code


async def verify_admin_pwd_otp(token: str, code: str, actor_id: str) -> bool:
    r = get_redis()
    rk = key("admin_pwd_otp", token)
    raw = await r.getdel(rk)
    if raw is None:
        return False
    payload = json.loads(raw)
    if payload["actor_id"] != actor_id:
        return False
    return secrets.compare_digest(payload["code_hash"], _otp_hash(code.strip()))


# ---------------------------------------------------------------------------
# OTP send rate limiting (Redis-backed sliding window)
# ---------------------------------------------------------------------------

async def check_otp_send_rate(rate_key: str) -> int:
    """Returns 0 if allowed, or seconds to wait if rate-limited."""
    if not settings.rate_limit_enabled:
        return 0
    now = time.time()
    r = get_redis()
    rk = key("otp_rate", rate_key)
    raw = await r.get(rk)
    stamps: list[float] = json.loads(raw) if raw else []
    stamps = [s for s in stamps if s > now - WINDOW]

    if stamps and now - stamps[-1] < COOLDOWN:
        return int(COOLDOWN - (now - stamps[-1])) + 1
    if len(stamps) >= MAX_PER_WINDOW:
        return int(WINDOW - (now - stamps[0])) + 1

    stamps.append(now)
    await r.setex(rk, WINDOW, json.dumps(stamps))
    return 0
