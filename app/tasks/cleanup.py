"""Daily database cleanup tasks scheduled via Celery Beat at 02:00 UTC.

Run a dedicated worker for the cleanup queue (concurrency=1 keeps tasks sequential
so bets are always archived before game sessions depend on them being gone):

    celery -A tasks.celery_app worker -Q cleanup --concurrency 1 --loglevel info

Run the beat scheduler (separate process):

    celery -A tasks.celery_app beat --loglevel info

Task dependency order enforced by queue FIFO with concurrency=1:
    archive_old_bets_job
        → archive_old_game_sessions_job  (requires bets already moved)
        → copy_old_wallet_transactions_job
        → auto_delete_inactive_accounts_job
        → process_stuck_pending_deletions_job
        → validate_wallet_integrity_job
"""
import logging
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

from core.config import settings
from services.hierarchy_service import HierarchyService
from tasks.celery_app import celery_app

log = logging.getLogger(__name__)

# Tunable age thresholds
BET_ARCHIVE_DAYS = 30
SESSION_ARCHIVE_DAYS = 30
WALLET_TX_ARCHIVE_DAYS = 90
INACTIVE_DELETE_DAYS = 30
STUCK_PENDING_DAYS = 30


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _conn() -> psycopg.Connection:
    """Open a fresh psycopg connection in autocommit mode (matches scheduler pattern)."""
    return psycopg.connect(settings.database_url, row_factory=dict_row, autocommit=True)


# ---------------------------------------------------------------------------
# 1. Archive old bets
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=300, time_limit=3600,
    name="tasks.cleanup.archive_old_bets_job",
)
def archive_old_bets_job(self) -> dict:
    """Move terminal bets (WON/LOST/REFUNDED) older than BET_ARCHIVE_DAYS to bets_archive.

    Uses a CTE so the INSERT and DELETE are atomic: only rows that were
    successfully inserted are deleted from the live table.
    PLACED bets are never touched.
    """
    try:
        cut = _cutoff(BET_ARCHIVE_DAYS)
        with _conn() as conn:
            with conn.transaction():
                cur = conn.execute(
                    """
                    WITH moved AS (
                        INSERT INTO bets_archive(
                            bet_id, session_id, player_id, side, amount, status, created_at
                        )
                        SELECT bet_id, session_id, player_id, side, amount, status, created_at
                        FROM   bets
                        WHERE  status IN ('WON', 'LOST', 'REFUNDED')
                          AND  created_at < %s
                        ON CONFLICT (bet_id) DO NOTHING
                        RETURNING bet_id
                    )
                    DELETE FROM bets WHERE bet_id IN (SELECT bet_id FROM moved)
                    """,
                    (cut,),
                )
                count = cur.rowcount
        log.info("archive_old_bets_job: archived %d bet(s) (cutoff %s)", count, cut.date())
        return {"archived": count}
    except Exception as exc:
        log.exception("archive_old_bets_job failed")
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# 2. Archive old game sessions
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=300, time_limit=3600,
    name="tasks.cleanup.archive_old_game_sessions_job",
)
def archive_old_game_sessions_job(self) -> dict:
    """Move COMPLETED/FAILED game sessions older than SESSION_ARCHIVE_DAYS to game_sessions_archive.

    A session is only eligible once all its bets have been moved out of the live
    bets table (by archive_old_bets_job).  Sessions with any remaining bet rows
    are left untouched — they will be picked up on the next daily run.

    Safety: without this guard, deleting a session while PLACED bets still
    reference it would silence _has_active_game(), potentially allowing premature
    account deletion.
    """
    try:
        cut = _cutoff(SESSION_ARCHIVE_DAYS)
        with _conn() as conn:
            with conn.transaction():
                cur = conn.execute(
                    """
                    WITH eligible AS (
                        SELECT gs.session_id
                        FROM   game_sessions gs
                        WHERE  gs.status IN ('COMPLETED', 'FAILED')
                          AND  gs.completed_at < %s
                          AND  NOT EXISTS (
                              SELECT 1 FROM bets b WHERE b.session_id = gs.session_id
                          )
                    ),
                    moved AS (
                        INSERT INTO game_sessions_archive(
                            session_id, game_key, status,
                            group_a_total, group_b_total,
                            winner, payload, created_at, completed_at
                        )
                        SELECT session_id, game_key, status,
                               group_a_total, group_b_total,
                               winner, payload, created_at, completed_at
                        FROM   game_sessions
                        WHERE  session_id IN (SELECT session_id FROM eligible)
                        ON CONFLICT (session_id) DO NOTHING
                        RETURNING session_id
                    )
                    DELETE FROM game_sessions WHERE session_id IN (SELECT session_id FROM moved)
                    """,
                    (cut,),
                )
                count = cur.rowcount
        log.info("archive_old_game_sessions_job: archived %d session(s) (cutoff %s)", count, cut.date())
        return {"archived": count}
    except Exception as exc:
        log.exception("archive_old_game_sessions_job failed")
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# 3. Copy old wallet transactions to archive (originals kept)
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=300, time_limit=3600,
    name="tasks.cleanup.copy_old_wallet_transactions_job",
)
def copy_old_wallet_transactions_job(self) -> dict:
    """Copy SUCCESS wallet transactions older than WALLET_TX_ARCHIVE_DAYS to
    wallet_transactions_archive.

    Originals are NOT deleted.  The idempotency_key UNIQUE index in
    wallet_transactions is the sole guard against double payouts; removing rows
    would allow the scheduler's recovery logic to re-process old bets and issue
    duplicate credits.  This task creates an archive copy for cold-storage /
    reporting without compromising that protection.
    """
    try:
        cut = _cutoff(WALLET_TX_ARCHIVE_DAYS)
        with _conn() as conn:
            with conn.transaction():
                cur = conn.execute(
                    """
                    INSERT INTO wallet_transactions_archive(
                        transaction_id, idempotency_key, transaction_type, direction,
                        from_wallet_id, to_wallet_id,
                        initiated_by_user_id, initiated_by_user_type,
                        amount, fee_amount, net_amount,
                        balance_before_from, balance_after_from,
                        balance_before_to,   balance_after_to,
                        reference_type, reference_id,
                        status, failure_reason, remarks,
                        created_at, completed_at
                    )
                    SELECT
                        transaction_id, idempotency_key, transaction_type, direction,
                        from_wallet_id, to_wallet_id,
                        initiated_by_user_id, initiated_by_user_type,
                        amount, fee_amount, net_amount,
                        balance_before_from, balance_after_from,
                        balance_before_to,   balance_after_to,
                        reference_type, reference_id,
                        status, failure_reason, remarks,
                        created_at, completed_at
                    FROM wallet_transactions
                    WHERE completed_at < %s
                      AND status = 'SUCCESS'
                    ON CONFLICT (transaction_id) DO NOTHING
                    """,
                    (cut,),
                )
                count = cur.rowcount
        log.info(
            "copy_old_wallet_transactions_job: copied %d transaction(s) to archive (cutoff %s)",
            count, cut.date(),
        )
        return {"copied": count}
    except Exception as exc:
        log.exception("copy_old_wallet_transactions_job failed")
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# 4. Auto-delete long-inactive accounts
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=300, time_limit=3600,
    name="tasks.cleanup.auto_delete_inactive_accounts_job",
)
def auto_delete_inactive_accounts_job(self) -> dict:
    """Delete INACTIVE account subtrees where the root has been deactivated for
    more than INACTIVE_DELETE_DAYS days.

    Pre-checks for every candidate (all must pass):
      1. status_changed_at is set (accounts deactivated before this column was
         added have NULL and are skipped — deactivation time is unknown).
      2. Every member of the subtree is INACTIVE with a status_changed_at older
         than INACTIVE_DELETE_DAYS (prevents cascade-deleting recently added
         child accounts).
      3. No PLACED bets in any active game session for any subtree member.
      4. Account is not already queued in pending_account_deletions (the game
         scheduler owns those rows — do not interfere).
      5. ADMIN and SYSTEM roles are never auto-deleted.
    """
    deleted = 0
    skipped = 0
    try:
        cut = _cutoff(INACTIVE_DELETE_DAYS)
        with _conn() as conn:
            # Find INACTIVE root accounts: parent is ACTIVE (or NULL), so this
            # is the topmost INACTIVE node in its branch.
            candidates = conn.execute(
                """
                SELECT a.id FROM accounts a
                WHERE  a.status = 'INACTIVE'
                  AND  a.status_changed_at IS NOT NULL
                  AND  a.status_changed_at < %s
                  AND  a.role NOT IN ('ADMIN', 'SYSTEM')
                  AND  a.id NOT IN (SELECT account_id FROM pending_account_deletions)
                  AND  (
                       a.parent_id IS NULL
                    OR EXISTS (
                           SELECT 1 FROM accounts p
                           WHERE p.id = a.parent_id AND p.status = 'ACTIVE'
                       )
                  )
                ORDER BY a.status_changed_at
                """,
                (cut,),
            ).fetchall()

            svc = HierarchyService(conn)
            for row in candidates:
                root_id = row["id"]
                subtree = svc._subtree_ids(root_id)

                # Every subtree member must be INACTIVE and aged-out.
                # An ACTIVE member or one with unknown/recent status_changed_at
                # means the subtree is not uniformly safe to delete.
                not_aged = conn.execute(
                    """
                    SELECT COUNT(*) AS cnt FROM accounts
                    WHERE id = ANY(%s)
                      AND (
                          status = 'ACTIVE'
                          OR status_changed_at IS NULL
                          OR status_changed_at >= %s
                      )
                    """,
                    (subtree, cut),
                ).fetchone()["cnt"]

                if not_aged:
                    log.info(
                        "auto_delete: skipping %s — %d subtree member(s) not aged-out",
                        root_id, not_aged,
                    )
                    skipped += 1
                    continue

                if svc._has_active_game(subtree):
                    log.info("auto_delete: skipping %s — active bets in subtree", root_id)
                    skipped += 1
                    continue

                svc._delete_accounts(subtree)
                log.info(
                    "auto_delete: deleted subtree rooted at %s (%d account(s))",
                    root_id, len(subtree),
                )
                deleted += len(subtree)

        log.info(
            "auto_delete_inactive_accounts_job: deleted=%d skipped=%d",
            deleted, skipped,
        )
        return {"deleted": deleted, "skipped": skipped}
    except Exception as exc:
        log.exception("auto_delete_inactive_accounts_job failed")
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# 5. Process stuck pending-deletion records
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=300, time_limit=1800,
    name="tasks.cleanup.process_stuck_pending_deletions_job",
)
def process_stuck_pending_deletions_job(self) -> dict:
    """Force-process pending_account_deletions records older than STUCK_PENDING_DAYS.

    The game scheduler already calls process_pending_deletions() after every
    game completion (~every 75 seconds).  A record this old means either the
    scheduler has been down for an extended period, or the subtree has bets that
    never settled (which would itself be a bug).  This task is a safety fallback.

    Behaviour mirrors HierarchyService.process_pending_deletions():
      - If the account no longer exists: remove the orphaned queue entry.
      - If the subtree still has active bets: log a warning and skip.
      - Otherwise: delete the subtree and remove the queue entry.
    """
    try:
        cut = _cutoff(STUCK_PENDING_DAYS)
        with _conn() as conn:
            stuck = conn.execute(
                "SELECT account_id FROM pending_account_deletions "
                "WHERE created_at < %s ORDER BY created_at",
                (cut,),
            ).fetchall()

            if not stuck:
                log.info("process_stuck_pending_deletions_job: nothing to process")
                return {"processed": 0, "skipped": 0}

            svc = HierarchyService(conn)
            processed = 0
            skipped = 0
            for row in stuck:
                account_id = row["account_id"]

                if not conn.execute(
                    "SELECT id FROM accounts WHERE id=%s", (account_id,)
                ).fetchone():
                    conn.execute(
                        "DELETE FROM pending_account_deletions WHERE account_id=%s",
                        (account_id,),
                    )
                    log.info("process_stuck: removed orphaned queue entry %s", account_id)
                    processed += 1
                    continue

                ids = svc._subtree_ids(account_id)
                if svc._has_active_game(ids):
                    log.warning(
                        "process_stuck: %s still has active bets after %d days — skipping",
                        account_id, STUCK_PENDING_DAYS,
                    )
                    skipped += 1
                    continue

                svc._delete_accounts(ids)
                conn.execute(
                    "DELETE FROM pending_account_deletions WHERE account_id=%s",
                    (account_id,),
                )
                log.info(
                    "process_stuck: deleted account %s and %d subtree member(s)",
                    account_id, len(ids),
                )
                processed += 1

        log.info(
            "process_stuck_pending_deletions_job: processed=%d skipped=%d",
            processed, skipped,
        )
        return {"processed": processed, "skipped": skipped}
    except Exception as exc:
        log.exception("process_stuck_pending_deletions_job failed")
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# 6. Wallet integrity validation (read-only)
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True, max_retries=2, default_retry_delay=300, time_limit=600,
    name="tasks.cleanup.validate_wallet_integrity_job",
)
def validate_wallet_integrity_job(self) -> dict:
    """Read-only integrity check — counts orphan wallets and orphan
    wallet_transaction references.  Logs ERROR if any are found; does NOT
    modify data.

    Orphan wallets are theoretically impossible (CASCADE DELETE on
    wallets.owner_id → accounts.id), but direct DB manipulation could create
    them.  Orphan wallet_transactions (dangling from_wallet_id / to_wallet_id)
    are also impossible through the application but are checked as a safeguard.
    """
    try:
        with _conn() as conn:
            orphan_wallets = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM wallets w
                LEFT JOIN accounts a ON a.id = w.owner_id
                WHERE a.id IS NULL
                """,
            ).fetchone()["cnt"]

            orphan_tx_from = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM wallet_transactions wt
                LEFT JOIN wallets w ON w.wallet_id = wt.from_wallet_id
                WHERE wt.from_wallet_id IS NOT NULL AND w.wallet_id IS NULL
                """,
            ).fetchone()["cnt"]

            orphan_tx_to = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM wallet_transactions wt
                LEFT JOIN wallets w ON w.wallet_id = wt.to_wallet_id
                WHERE wt.to_wallet_id IS NOT NULL AND w.wallet_id IS NULL
                """,
            ).fetchone()["cnt"]

        if orphan_wallets:
            log.error(
                "WALLET INTEGRITY: %d orphan wallet(s) — owner_id has no matching account",
                orphan_wallets,
            )
        if orphan_tx_from or orphan_tx_to:
            log.error(
                "WALLET INTEGRITY: orphan wallet_transactions — "
                "from_wallet: %d  to_wallet: %d",
                orphan_tx_from, orphan_tx_to,
            )
        if not (orphan_wallets or orphan_tx_from or orphan_tx_to):
            log.info("validate_wallet_integrity_job: all checks passed — no orphans found")

        return {
            "orphan_wallets": orphan_wallets,
            "orphan_tx_from_wallet": orphan_tx_from,
            "orphan_tx_to_wallet": orphan_tx_to,
            "integrity_ok": not (orphan_wallets or orphan_tx_from or orphan_tx_to),
        }
    except Exception as exc:
        log.exception("validate_wallet_integrity_job failed")
        raise self.retry(exc=exc)


# ---------------------------------------------------------------------------
# 7. Master daily orchestrator
# ---------------------------------------------------------------------------

@celery_app.task(name="tasks.cleanup.daily_cleanup_job")
def daily_cleanup_job() -> dict:
    """Enqueue all cleanup tasks in dependency order onto the 'cleanup' queue.

    With --concurrency 1 on the cleanup worker, the queue is FIFO, so bets are
    always archived before game_sessions attempts to archive their parent sessions.
    Wallet transaction copy, account auto-delete, stuck-pending processing, and
    integrity check are independent of that ordering and run after.
    """
    log.info("daily_cleanup_job: queuing daily cleanup sequence")
    archive_old_bets_job.apply_async(queue="cleanup")
    archive_old_game_sessions_job.apply_async(queue="cleanup")
    copy_old_wallet_transactions_job.apply_async(queue="cleanup")
    auto_delete_inactive_accounts_job.apply_async(queue="cleanup")
    process_stuck_pending_deletions_job.apply_async(queue="cleanup")
    validate_wallet_integrity_job.apply_async(queue="cleanup")
    log.info("daily_cleanup_job: 6 task(s) queued")
    return {"status": "queued", "tasks": 6}
