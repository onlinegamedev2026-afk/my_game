import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote as _urlquote


def _load_dotenv() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _build_database_url() -> str:
    """Construct DATABASE_URL from DB_* parts so only one place needs updating.

    Priority:
      1. DATABASE_URL env var (set explicitly — docker-compose overrides use this)
      2. Assembled from DB_USER + DB_PASSWORD + DB_HOST + DB_PORT + DB_NAME

    User and password are percent-encoded so any character is safe:
    @, #, $, %, /, : all work without breaking the URL.
    """
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        return explicit
    user = _urlquote(os.getenv("DB_USER", "dev_user"), safe="")
    password = _urlquote(os.getenv("DB_PASSWORD", "dev_pass"), safe="")
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "luck_game_v2")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


@dataclass(frozen=True)
class Settings:
    # App
    app_name: str = os.getenv("APP_NAME", "Luck Game")
    app_env: str = os.getenv("APP_ENV", "development")  # development | local-prod | production
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # Security
    secret_key: str = os.getenv("SECRET_KEY", "dev-secret-key-change-me-in-production")
    csrf_secret: str = os.getenv("CSRF_SECRET", "dev-csrf-secret-change-me")
    cookie_secure: bool = os.getenv("COOKIE_SECURE", "false").lower() in {"1", "true", "yes", "on"}

    # Database — PostgreSQL only in v2
    database_url: str = _build_database_url()
    db_pool_min: int = int(os.getenv("DB_POOL_MIN", "2"))
    db_pool_max: int = int(os.getenv("DB_POOL_MAX", "10"))

    # Redis
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    celery_broker_url: str = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1")
    celery_result_backend: str = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")
    redis_pubsub_channel: str = os.getenv("REDIS_PUBSUB_CHANNEL", "luck_game_events")

    # Admin seed
    admin_username: str = os.getenv("ADMIN_USERNAME", "admin")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "admin123")
    admin_email_id: str = os.getenv("ADMIN_EMAIL_ID", "admin@example.com")

    # SMTP
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_username: str = os.getenv("SMTP_USERNAME", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from_email: str = os.getenv("SMTP_FROM_EMAIL", os.getenv("SMTP_USERNAME", ""))
    smtp_use_tls: bool = os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes", "on"}
    smtp_delete_sent_copy: bool = os.getenv("SMTP_DELETE_SENT_COPY", "true").lower() in {"1", "true", "yes", "on"}
    smtp_imap_host: str = os.getenv("SMTP_IMAP_HOST", "imap.gmail.com")
    smtp_imap_port: int = int(os.getenv("SMTP_IMAP_PORT", "993"))
    smtp_sent_mailbox: str = os.getenv("SMTP_SENT_MAILBOX", "[Gmail]/Sent Mail")

    # Game timing
    betting_window_seconds: int = int(os.getenv("BETTING_WINDOW_SECONDS", "40"))
    game_initiation_seconds: int = int(os.getenv("GAME_INITIATION_SECONDS", "10"))
    after_game_cooldown_seconds: int = int(os.getenv("AFTER_GAME_COOLDOWN_SECONDS", "10"))
    card_drawing_delay_seconds: float = float(os.getenv("CARD_DRAWING_DELAY_SECONDS", "3"))

    # Game scheduler
    game_scheduler_enabled: bool = os.getenv("GAME_SCHEDULER_ENABLED", "false").lower() in {"1", "true", "yes", "on"}

    # Session timeout — 1-10 hours; -1 for no timeout
    session_timeout_hour: int = int(os.getenv("SESSION_TIMEOUT_HOUR", "8"))

    # Rate limiting
    rate_limit_enabled: bool = os.getenv("RATE_LIMIT_ENABLED", "true").lower() in {"1", "true", "yes", "on"}

    # Money
    min_bet: Decimal = Decimal("10.000")
    payout_fee_rate: Decimal = Decimal("0.050")

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_dev(self) -> bool:
        return self.app_env == "development"

    @property
    def show_dev_otp(self) -> bool:
        return self.is_dev and not self.smtp_host


settings = Settings()
