"""Redis-backed CAPTCHA service — replaces the CAPTCHA_STORE in-process dict."""
import secrets

from core.redis_client import get_redis, key

_CAPTCHA_TTL = 600  # 10 minutes


async def make_captcha() -> dict[str, str]:
    left = secrets.randbelow(8) + 2
    right = secrets.randbelow(8) + 2
    token = secrets.token_urlsafe(24)
    r = get_redis()
    await r.setex(key("captcha", token), _CAPTCHA_TTL, str(left + right))
    return {"token": token, "question": f"{left} + {right}"}


async def verify_captcha(token: str, answer: str) -> bool:
    r = get_redis()
    rk = key("captcha", token)
    stored = await r.getdel(rk)
    if stored is None:
        return False
    try:
        return int(answer.strip()) == int(stored)
    except ValueError:
        return False
