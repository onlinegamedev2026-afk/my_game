import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from core.config import settings


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 150000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        salt, stored = password_hash.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 150000).hex()
    return hmac.compare_digest(digest, stored)


def sign_session(user_id: str, role: str, nonce: str) -> str:
    hours = settings.session_timeout_hour
    if hours == -1:
        delta = timedelta(days=365 * 10)  # effectively infinite
    else:
        delta = timedelta(hours=max(1, min(10, hours)))
    expires = int((datetime.utcnow() + delta).timestamp())
    payload = f"{user_id}:{role}:{expires}:{nonce}"
    sig = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def session_timeout_seconds() -> int | None:
    """Returns TTL in seconds for Redis; None means no expiry."""
    hours = settings.session_timeout_hour
    if hours == -1:
        return None
    return max(1, min(10, hours)) * 3600


def read_session(token: str | None) -> tuple[str, str, str] | None:
    if not token:
        return None
    parts = token.split(":")
    if len(parts) != 5:
        return None
    user_id, role, expires, nonce, sig = parts
    payload = f"{user_id}:{role}:{expires}:{nonce}"
    expected = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    if int(expires) < int(datetime.utcnow().timestamp()):
        return None
    return user_id, role, nonce


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

def generate_csrf_token(session_sig: str) -> str:
    """Generate a per-session CSRF token bound to the session signature."""
    nonce = secrets.token_hex(16)
    payload = f"{nonce}:{session_sig}"
    tag = hmac.new(settings.csrf_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{nonce}:{tag}"


def verify_csrf_token(token: str | None, session_sig: str) -> bool:
    if not token:
        return False
    parts = token.split(":", 1)
    if len(parts) != 2:
        return False
    nonce, tag = parts
    payload = f"{nonce}:{session_sig}"
    expected = hmac.new(settings.csrf_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, tag)
