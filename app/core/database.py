import uuid
import logging

import psycopg
import psycopg_pool
from psycopg.rows import dict_row

from core.config import settings
from core.security import hash_password
from utils.money import money_str

log = logging.getLogger(__name__)

_pool: psycopg_pool.ConnectionPool | None = None


def get_pool() -> psycopg_pool.ConnectionPool:
    global _pool
    if _pool is None:
        raise RuntimeError("Database pool not initialised — call init_pool() first.")
    return _pool


def init_pool() -> None:
    global _pool
    if _pool is not None:
        return
    _pool = psycopg_pool.ConnectionPool(
        conninfo=settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        kwargs={"row_factory": dict_row, "autocommit": False, "prepare_threshold": None},
        open=True,
    )
    log.info("PostgreSQL connection pool opened (min=%d max=%d)", settings.db_pool_min, settings.db_pool_max)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
        log.info("PostgreSQL connection pool closed.")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    email TEXT NULL,
    role TEXT NOT NULL CHECK(role IN ('ADMIN','AGENT','USER','SYSTEM')),
    password_hash TEXT NOT NULL,
    parent_id TEXT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','INACTIVE')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS wallets (
    wallet_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL UNIQUE REFERENCES accounts(id) ON DELETE CASCADE,
    owner_type TEXT NOT NULL CHECK(owner_type IN ('ADMIN','AGENT','USER','SYSTEM')),
    current_balance NUMERIC(18,3) NOT NULL DEFAULT 0.000,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','LOCKED','FROZEN','CLOSED')),
    version INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS wallet_transactions (
    transaction_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    transaction_type TEXT NOT NULL,
    direction TEXT NOT NULL,
    from_wallet_id TEXT NULL REFERENCES wallets(wallet_id),
    to_wallet_id TEXT NULL REFERENCES wallets(wallet_id),
    initiated_by_user_id TEXT NOT NULL,
    initiated_by_user_type TEXT NOT NULL,
    amount NUMERIC(18,3) NOT NULL,
    fee_amount NUMERIC(18,3) NOT NULL DEFAULT 0.000,
    net_amount NUMERIC(18,3) NOT NULL,
    balance_before_from NUMERIC(18,3) NULL,
    balance_after_from NUMERIC(18,3) NULL,
    balance_before_to NUMERIC(18,3) NULL,
    balance_after_to NUMERIC(18,3) NULL,
    reference_type TEXT NULL,
    reference_id TEXT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    failure_reason TEXT NULL,
    remarks TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS bets (
    bet_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    player_id TEXT NOT NULL REFERENCES accounts(id),
    side TEXT NOT NULL CHECK(side IN ('A','B')),
    amount NUMERIC(18,3) NOT NULL,
    status TEXT NOT NULL DEFAULT 'PLACED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS game_sessions (
    session_id TEXT PRIMARY KEY,
    game_key TEXT NOT NULL,
    status TEXT NOT NULL,
    group_a_total NUMERIC(18,3) NOT NULL DEFAULT 0.000,
    group_b_total NUMERIC(18,3) NOT NULL DEFAULT 0.000,
    winner TEXT NULL,
    payload JSONB NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS pending_account_deletions (
    account_id TEXT PRIMARY KEY,
    requested_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_accounts_parent ON accounts(parent_id);
CREATE INDEX IF NOT EXISTS idx_accounts_email ON accounts(LOWER(TRIM(email))) WHERE email IS NOT NULL AND TRIM(email) <> '';
CREATE INDEX IF NOT EXISTS idx_bets_session ON bets(session_id);
CREATE INDEX IF NOT EXISTS idx_bets_player_status ON bets(player_id, status);
CREATE INDEX IF NOT EXISTS idx_game_sessions_status ON game_sessions(status, game_key);
CREATE INDEX IF NOT EXISTS idx_wallet_tx_from ON wallet_transactions(from_wallet_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_tx_to ON wallet_transactions(to_wallet_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_email_unique
    ON accounts(LOWER(TRIM(email)))
    WHERE email IS NOT NULL AND TRIM(email) <> '';

-- Archive tables: no FK constraints so archived rows survive account/wallet deletion
CREATE TABLE IF NOT EXISTS bets_archive (
    bet_id       TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    player_id    TEXT NOT NULL,
    side         TEXT NOT NULL,
    amount       NUMERIC(18,3) NOT NULL,
    status       TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL,
    archived_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS game_sessions_archive (
    session_id      TEXT PRIMARY KEY,
    game_key        TEXT NOT NULL,
    status          TEXT NOT NULL,
    group_a_total   NUMERIC(18,3) NOT NULL DEFAULT 0.000,
    group_b_total   NUMERIC(18,3) NOT NULL DEFAULT 0.000,
    winner          TEXT NULL,
    payload         JSONB NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    completed_at    TIMESTAMPTZ NULL,
    archived_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS wallet_transactions_archive (
    transaction_id          TEXT PRIMARY KEY,
    idempotency_key         TEXT NOT NULL,
    transaction_type        TEXT NOT NULL,
    direction               TEXT NOT NULL,
    from_wallet_id          TEXT NULL,
    to_wallet_id            TEXT NULL,
    initiated_by_user_id    TEXT NOT NULL,
    initiated_by_user_type  TEXT NOT NULL,
    amount                  NUMERIC(18,3) NOT NULL,
    fee_amount              NUMERIC(18,3) NOT NULL DEFAULT 0.000,
    net_amount              NUMERIC(18,3) NOT NULL,
    balance_before_from     NUMERIC(18,3) NULL,
    balance_after_from      NUMERIC(18,3) NULL,
    balance_before_to       NUMERIC(18,3) NULL,
    balance_after_to        NUMERIC(18,3) NULL,
    reference_type          TEXT NULL,
    reference_id            TEXT NULL,
    status                  TEXT NOT NULL,
    failure_reason          TEXT NULL,
    remarks                 TEXT NULL,
    created_at              TIMESTAMPTZ NOT NULL,
    completed_at            TIMESTAMPTZ NULL,
    archived_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Track when an account was last deactivated so auto-delete can enforce the 30-day window
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS status_changed_at TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS idx_accounts_inactive_age
    ON accounts(status_changed_at) WHERE status = 'INACTIVE';

CREATE INDEX IF NOT EXISTS idx_bets_archive_player  ON bets_archive(player_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bets_archive_session ON bets_archive(session_id);

CREATE INDEX IF NOT EXISTS idx_game_sessions_archive_completed
    ON game_sessions_archive(completed_at DESC, game_key);

CREATE INDEX IF NOT EXISTS idx_wallet_tx_archive_from ON wallet_transactions_archive(from_wallet_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_tx_archive_to   ON wallet_transactions_archive(to_wallet_id,   created_at DESC);
"""


def init_db() -> None:
    with get_pool().connection() as conn:
        conn.autocommit = True
        conn.execute(_SCHEMA_SQL)
        _ensure_seed_data(conn)
    log.info("Database schema initialised.")


def _ensure_seed_data(conn: psycopg.Connection) -> None:
    admin = conn.execute("SELECT id FROM accounts WHERE role='ADMIN'").fetchone()
    if admin:
        conn.execute(
            "UPDATE accounts SET username=%s, email=%s, password_hash=%s WHERE role='ADMIN'",
            (settings.admin_username, settings.admin_email_id, hash_password(settings.admin_password)),
        )
        return

    admin_id = settings.admin_username
    wallet_id = "admin_wallet"
    system_id = str(uuid.uuid4())
    system_wallet_id = str(uuid.uuid4())

    with conn.transaction():
        conn.execute(
            "INSERT INTO accounts(id, username, display_name, email, role, password_hash, parent_id) VALUES(%s,%s,%s,%s,%s,%s,NULL)",
            (admin_id, settings.admin_username, "Main Admin", settings.admin_email_id, "ADMIN", hash_password(settings.admin_password)),
        )
        conn.execute(
            "INSERT INTO wallets(wallet_id, owner_id, owner_type, current_balance) VALUES(%s,%s,%s,%s)",
            (wallet_id, admin_id, "ADMIN", 0),
        )
        conn.execute(
            "INSERT INTO accounts(id, username, display_name, email, role, password_hash, parent_id) VALUES(%s,%s,%s,%s,%s,%s,NULL)",
            (system_id, "system_pool", "System Game Pool", "", "SYSTEM", hash_password(uuid.uuid4().hex)),
        )
        conn.execute(
            "INSERT INTO wallets(wallet_id, owner_id, owner_type, current_balance) VALUES(%s,%s,%s,%s)",
            (system_wallet_id, system_id, "SYSTEM", 0),
        )
