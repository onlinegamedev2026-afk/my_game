"""Standalone game scheduler — runs as its own Docker container.

Responsibilities:
- Own the full game cycle for each game (open betting → initiate → run → settle → idle).
- Acquire a Redis distributed lock so only ONE scheduler instance is active per game.
- Write game results to PostgreSQL.
- Broadcast events through Redis Pub/Sub so all app containers relay them to WebSocket clients.
- Refresh its lock heartbeat regularly to retain ownership.

Entry point:
    python -m scheduler.game_scheduler
"""
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path when run as __main__
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.config import settings
from core.database import init_pool, init_db, get_pool
from core.logging_config import configure_logging
from core.redis_client import init_redis, get_redis, key as rk, DistributedLock
from games.andar_bahar import AndarBaharGame
from games.color_guessing import ColorGuessingGame
from games.tin_patti import TinPattiGame
from services.hierarchy_service import HierarchyService
from utils.money import money

configure_logging()
log = logging.getLogger("scheduler")

CHANNEL = settings.redis_pubsub_channel

GAME_DEFINITIONS: dict[str, dict[str, Any]] = {
    "tin-patti": {
        "db_key": "TIN_PATTI",
        "title": "Teen Patti",
        "engine": TinPattiGame,
        "total_draws": 6,
        "has_joker": False,
    },
    "andar-bahar": {
        "db_key": "ANDAR_BAHAR",
        "title": "Andar Bahar",
        "engine": AndarBaharGame,
        "total_draws": None,
        "has_joker": True,
    },
    "color-guessing": {
        "db_key": "COLOR_GUESSING",
        "title": "Color Guess",
        "engine": ColorGuessingGame,
        "total_draws": 1,
        "has_joker": False,
        "is_color_game": True,
    },
}

# -------------------------------------------------------------------------
# Redis state writers
# -------------------------------------------------------------------------

def _gk(game_key: str, field: str) -> str:
    return rk("game", game_key, field)


async def _set_state(game_key: str, **fields: Any) -> None:
    r = get_redis()
    pipe = r.pipeline()
    for field, value in fields.items():
        k = _gk(game_key, field)
        if value is None:
            pipe.delete(k)
        elif isinstance(value, (dict, list)):
            pipe.set(k, json.dumps(value))
        else:
            pipe.set(k, str(value))
    await pipe.execute()


async def _publish(event: str, data: dict, roles: list[str] | None = None) -> None:
    r = get_redis()
    await r.publish(CHANNEL, json.dumps({"event": event, "data": data, "roles": roles}))


# -------------------------------------------------------------------------
# Pool wallet helper
# -------------------------------------------------------------------------

def _pool_wallet(conn) -> str:
    row = conn.execute(
        "SELECT w.wallet_id FROM accounts a JOIN wallets w ON w.owner_id=a.id WHERE a.username='system_pool'"
    ).fetchone()
    return row["wallet_id"]


# -------------------------------------------------------------------------
# Bet settlement (with idempotency and SELECT FOR UPDATE)
# -------------------------------------------------------------------------

def _settle_bets(conn, session_id: str, winner: str) -> None:
    if winner not in {"A", "B"}:
        return
    pool_wallet = _pool_wallet(conn)
    bets = conn.execute(
        "SELECT * FROM bets WHERE session_id=%s AND status='PLACED'", (session_id,)
    ).fetchall()

    # Calculate payout needed and top-up pool if necessary
    required_payout = Decimal("0.000")
    for bet in bets:
        if bet["side"] == winner:
            bet_amount = money(bet["amount"])
            fee = money(bet_amount * settings.payout_fee_rate)
            required_payout = money(required_payout + bet_amount + (bet_amount - fee))

    if required_payout > 0:
        _ensure_pool_balance(conn, pool_wallet, required_payout, session_id)

    for bet in bets:
        if bet["side"] != winner:
            conn.execute("UPDATE bets SET status='LOST' WHERE bet_id=%s", (bet["bet_id"],))
            continue

        # Check idempotency — if payout already done, skip
        idem_key = f"payout:{bet['bet_id']}"
        existing = conn.execute(
            "SELECT transaction_id FROM wallet_transactions WHERE idempotency_key=%s AND status='SUCCESS'",
            (idem_key,),
        ).fetchone()
        if existing:
            conn.execute("UPDATE bets SET status='WON' WHERE bet_id=%s", (bet["bet_id"],))
            continue

        player_wallet_row = conn.execute(
            "SELECT wallet_id FROM wallets WHERE owner_id=%s", (bet["player_id"],)
        ).fetchone()
        if not player_wallet_row:
            conn.execute("UPDATE bets SET status='LOST' WHERE bet_id=%s", (bet["bet_id"],))
            continue

        bet_amount = money(bet["amount"])
        fee = money(bet_amount * settings.payout_fee_rate)
        payout = money(bet_amount + (bet_amount - fee))
        tx_id = str(uuid.uuid4())
        player_wallet = player_wallet_row["wallet_id"]
        wallet_ids = sorted([pool_wallet, player_wallet])

        with conn.transaction():
            rows = conn.execute(
                "SELECT * FROM wallets WHERE wallet_id = ANY(%s) ORDER BY wallet_id FOR UPDATE",
                (wallet_ids,),
            ).fetchall()
            wallets = {r["wallet_id"]: r for r in rows}
            pw = wallets[pool_wallet]
            cw = wallets[player_wallet]
            before_pool = money(pw["current_balance"])
            before_player = money(cw["current_balance"])
            actual_payout = min(payout, before_pool)
            after_pool = money(before_pool - actual_payout)
            after_player = money(before_player + actual_payout)

            conn.execute(
                """
                INSERT INTO wallet_transactions(
                    transaction_id, idempotency_key, transaction_type, direction,
                    from_wallet_id, to_wallet_id, initiated_by_user_id, initiated_by_user_type,
                    amount, fee_amount, net_amount, balance_before_from, balance_after_from,
                    balance_before_to, balance_after_to, reference_type, reference_id,
                    status, completed_at
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'SUCCESS',NOW())
                """,
                (
                    tx_id, idem_key, "BET_WIN_CREDIT", "TRANSFER",
                    pool_wallet, player_wallet,
                    "system", "SYSTEM",
                    float(actual_payout), float(fee), float(actual_payout),
                    float(before_pool), float(after_pool),
                    float(before_player), float(after_player),
                    "GAME_SESSION", session_id,
                ),
            )
            conn.execute(
                "UPDATE wallets SET current_balance=%s, version=version+1, updated_at=NOW() WHERE wallet_id=%s",
                (float(after_pool), pool_wallet),
            )
            conn.execute(
                "UPDATE wallets SET current_balance=%s, version=version+1, updated_at=NOW() WHERE wallet_id=%s",
                (float(after_player), player_wallet),
            )
            conn.execute("UPDATE bets SET status='WON' WHERE bet_id=%s", (bet["bet_id"],))


def _ensure_pool_balance(conn, pool_wallet: str, required: Decimal, session_id: str) -> None:
    with conn.transaction():
        row = conn.execute(
            "SELECT current_balance FROM wallets WHERE wallet_id=%s FOR UPDATE", (pool_wallet,)
        ).fetchone()
        before = money(row["current_balance"])
        if before >= required:
            return
        top_up = money(required - before)
        after = money(before + top_up)
        tx_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO wallet_transactions(
                transaction_id, idempotency_key, transaction_type, direction,
                from_wallet_id, to_wallet_id, initiated_by_user_id, initiated_by_user_type,
                amount, fee_amount, net_amount, balance_before_to, balance_after_to,
                reference_type, reference_id, status, remarks, completed_at
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'SUCCESS',%s,NOW())
            """,
            (
                tx_id, f"pool-topup:{session_id}:{uuid.uuid4()}",
                "SYSTEM_POOL_TOPUP", "CREDIT",
                None, pool_wallet, "system", "SYSTEM",
                float(top_up), 0.0, float(top_up),
                float(before), float(after),
                "GAME_SESSION", session_id,
                "Automatic game pool reserve top-up",
            ),
        )
        conn.execute(
            "UPDATE wallets SET current_balance=%s, version=version+1, updated_at=NOW() WHERE wallet_id=%s",
            (float(after), pool_wallet),
        )


# -------------------------------------------------------------------------
# Game cycle (one game key)
# -------------------------------------------------------------------------

async def run_game_cycle(game_key: str) -> None:
    defn = GAME_DEFINITIONS[game_key]
    db_key = defn["db_key"]
    lock = DistributedLock(f"game:{game_key}", ttl_ms=90_000)

    log.info("[%s] Trying to acquire scheduler lock …", game_key)
    while not await lock.acquire():
        log.debug("[%s] Lock held by another instance, waiting …", game_key)
        await asyncio.sleep(5)
    log.info("[%s] Lock acquired.", game_key)

    try:
        while True:
            await _run_one_cycle(game_key, db_key, defn, lock)
    except asyncio.CancelledError:
        pass
    finally:
        await lock.release()
        log.info("[%s] Lock released.", game_key)


async def _run_one_cycle(game_key: str, db_key: str, defn: dict, lock: DistributedLock) -> None:
    session_id = str(uuid.uuid4())
    try:
        # --- BETTING phase ---
        with get_pool().connection() as conn:
            conn.autocommit = True
            conn.execute(
                "INSERT INTO game_sessions(session_id, game_key, status) VALUES(%s,%s,'BETTING')",
                (session_id, db_key),
            )
        await _set_state(
            game_key,
            session_id=session_id,
            phase="BETTING",
            phase_ends_at=time.time() + settings.betting_window_seconds,
            cards_dealt=None,
            winner=None,
            joker=None,
            winning_card=None,
        )
        await _publish("betting_opened", {
            "game_key": game_key,
            "session_id": session_id,
            "seconds": settings.betting_window_seconds,
            "remaining_seconds": settings.betting_window_seconds,
            "phase_ends_at": time.time() + settings.betting_window_seconds,
        })
        await lock.refresh()
        await asyncio.sleep(settings.betting_window_seconds)

        # --- INITIATING phase ---
        with get_pool().connection() as conn:
            conn.autocommit = True
            conn.execute("UPDATE game_sessions SET status='INITIATING' WHERE session_id=%s", (session_id,))
            totals = conn.execute(
                "SELECT group_a_total, group_b_total FROM game_sessions WHERE session_id=%s", (session_id,)
            ).fetchone()
        await _set_state(game_key, phase="INITIATING", phase_ends_at=time.time() + settings.game_initiation_seconds)
        await _publish("game_initiating", {
            "game_key": game_key,
            "seconds": settings.game_initiation_seconds,
            "remaining_seconds": settings.game_initiation_seconds,
            "phase_ends_at": time.time() + settings.game_initiation_seconds,
        })
        await _publish("betting_totals", {
            "game_key": game_key,
            "group_a_total": f"{totals['group_a_total']:.3f}",
            "group_b_total": f"{totals['group_b_total']:.3f}",
        }, roles=["ADMIN"])
        await lock.refresh()
        await asyncio.sleep(settings.game_initiation_seconds)

        # --- RUNNING phase ---
        with get_pool().connection() as conn:
            conn.autocommit = True
            session_row = conn.execute(
                "SELECT * FROM game_sessions WHERE session_id=%s", (session_id,)
            ).fetchone()
        await _set_state(game_key, phase="RUNNING", phase_ends_at=None)
        await _publish("game_started", {"game_key": game_key, "delay": settings.card_drawing_delay_seconds})

        result = defn["engine"]().play(float(session_row["group_a_total"]), float(session_row["group_b_total"]))

        with get_pool().connection() as conn:
            conn.autocommit = True
            conn.execute(
                "UPDATE game_sessions SET status='RUNNING', payload=%s WHERE session_id=%s",
                (json.dumps(result), session_id),
            )
        await lock.refresh()

        if defn.get("is_color_game"):
            await asyncio.sleep(settings.card_drawing_delay_seconds)
            await _set_state(game_key, winner=result["WINNER"])
            await _publish("color_revealed", {
                "game_key": game_key,
                "winner": result["WINNER"],
                "color": result["COLOR"],
            })
        elif defn["has_joker"]:
            joker_dict = {"rank": result["JOKER"][0], "suit": result["JOKER"][1]}
            await _set_state(game_key, joker=joker_dict)
            await asyncio.sleep(settings.card_drawing_delay_seconds)
            await _publish("joker_opened", {"game_key": game_key, "joker": joker_dict})
            cards_dealt = await _deal_andar_bahar(game_key, result)
        else:
            cards_dealt = await _deal_tin_patti(game_key, result)

        if not defn.get("is_color_game"):
            winner_card = result.get("WINNING_CARD")
            wc_dict = {"rank": winner_card[0], "suit": winner_card[1]} if winner_card else None
            await _set_state(game_key, winner=result["WINNER"], winning_card=wc_dict)

        winner = str(result["WINNER"])

        # Settlement
        with get_pool().connection() as conn:
            conn.autocommit = False
            _settle_bets(conn, session_id, winner)
            conn.execute(
                "UPDATE game_sessions SET status='COMPLETED', winner=%s, completed_at=NOW() WHERE session_id=%s",
                (winner, session_id),
            )
            conn.commit()

        with get_pool().connection() as conn:
            conn.autocommit = True
            HierarchyService(conn).process_pending_deletions()
            last_10_rows = conn.execute(
                """
                SELECT winner FROM game_sessions
                WHERE game_key=%s AND status='COMPLETED' AND winner IS NOT NULL
                ORDER BY completed_at DESC LIMIT 10
                """,
                (db_key,),
            ).fetchall()
            last_10_winners = [row["winner"] for row in reversed(last_10_rows)]

        await _publish("game_result", {
            "game_key": game_key,
            "winner": winner,
            "time": result.get("TIME", ""),
            "winning_card": (await _get_state(game_key, "winning_card")),
            "last_10_winners": last_10_winners,
        })

        # --- SETTLING phase ---
        await _set_state(game_key, phase="SETTLING", phase_ends_at=time.time() + settings.after_game_cooldown_seconds)
        with get_pool().connection() as conn:
            conn.autocommit = True
            totals = conn.execute(
                "SELECT group_a_total, group_b_total FROM game_sessions WHERE session_id=%s", (session_id,)
            ).fetchone()
        await _publish("settlement_cooldown", {
            "game_key": game_key,
            "seconds": settings.after_game_cooldown_seconds,
            "remaining_seconds": settings.after_game_cooldown_seconds,
        })
        await _publish("betting_totals", {
            "game_key": game_key,
            "group_a_total": f"{totals['group_a_total']:.3f}",
            "group_b_total": f"{totals['group_b_total']:.3f}",
        }, roles=["ADMIN"])
        await lock.refresh()
        await asyncio.sleep(settings.after_game_cooldown_seconds)

    except Exception as exc:
        log.exception("[%s] Cycle error: %s", game_key, exc)
        try:
            with get_pool().connection() as conn:
                conn.autocommit = True
                conn.execute(
                    "UPDATE game_sessions SET status='FAILED', completed_at=NOW() WHERE session_id=%s",
                    (session_id,),
                )
        except Exception:
            pass
        await _publish("game_error", {"game_key": game_key, "message": str(exc)})
        await asyncio.sleep(2)
    finally:
        # Always return to IDLE, clear session
        await _set_state(game_key, phase="IDLE", phase_ends_at=None, session_id=None)
        await _publish("cycle_complete", {"game_key": game_key})


async def _get_state(game_key: str, field: str) -> Any:
    v = await get_redis().get(_gk(game_key, field))
    if v is None:
        return None
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        return v


async def _deal_tin_patti(game_key: str, result: dict) -> list[dict]:
    cards_a, cards_b = result["A"], result["B"]
    dealt: list[dict] = []
    for i in range(3):
        await asyncio.sleep(settings.card_drawing_delay_seconds)
        event = {"game_key": game_key, "group": "A", "rank": cards_a[i][0], "suit": cards_a[i][1], "draw_num": i*2+1, "total_draws": 6}
        dealt.append(event)
        await get_redis().set(_gk(game_key, "cards_dealt"), json.dumps(dealt))
        await _publish("card_dealt", event)

        await asyncio.sleep(settings.card_drawing_delay_seconds)
        event = {"game_key": game_key, "group": "B", "rank": cards_b[i][0], "suit": cards_b[i][1], "draw_num": i*2+2, "total_draws": 6}
        dealt.append(event)
        await get_redis().set(_gk(game_key, "cards_dealt"), json.dumps(dealt))
        await _publish("card_dealt", event)
    return dealt


async def _deal_andar_bahar(game_key: str, result: dict) -> list[dict]:
    cards = {"A": list(result["A"]), "B": list(result["B"])}
    indexes = {"A": 0, "B": 0}
    deal_order = result.get("DEAL_ORDER") or []
    total_draws = int(result.get("TOTAL_DRAWS") or len(deal_order))
    dealt: list[dict] = []
    for draw_num, group in enumerate(deal_order, start=1):
        await asyncio.sleep(settings.card_drawing_delay_seconds)
        card = cards[group][indexes[group]]
        indexes[group] += 1
        event = {"game_key": game_key, "group": group, "rank": card[0], "suit": card[1], "draw_num": draw_num, "total_draws": total_draws}
        dealt.append(event)
        await get_redis().set(_gk(game_key, "cards_dealt"), json.dumps(dealt))
        await _publish("card_dealt", event)
    return dealt


# -------------------------------------------------------------------------
# Startup: refund bets from sessions interrupted before this boot
# -------------------------------------------------------------------------

def recover_interrupted_sessions() -> None:
    with get_pool().connection() as conn:
        conn.autocommit = False
        pool_row = conn.execute(
            "SELECT w.wallet_id FROM accounts a JOIN wallets w ON w.owner_id=a.id WHERE a.username='system_pool'"
        ).fetchone()
        if not pool_row:
            return
        pool_wallet = pool_row["wallet_id"]

        stuck = conn.execute(
            "SELECT session_id FROM game_sessions WHERE status NOT IN ('COMPLETED','FAILED')"
        ).fetchall()

        for sr in stuck:
            sid = sr["session_id"]
            bets = conn.execute(
                "SELECT * FROM bets WHERE session_id=%s AND status='PLACED'", (sid,)
            ).fetchall()
            for bet in bets:
                pw_row = conn.execute(
                    "SELECT wallet_id FROM wallets WHERE owner_id=%s", (bet["player_id"],)
                ).fetchone()
                if not pw_row:
                    conn.execute("UPDATE bets SET status='REFUNDED' WHERE bet_id=%s", (bet["bet_id"],))
                    conn.commit()
                    continue
                player_wallet = pw_row["wallet_id"]
                bet_amount = money(bet["amount"])
                idem_key = f"refund:{bet['bet_id']}"
                existing = conn.execute(
                    "SELECT 1 FROM wallet_transactions WHERE idempotency_key=%s", (idem_key,)
                ).fetchone()
                if existing:
                    conn.execute("UPDATE bets SET status='REFUNDED' WHERE bet_id=%s", (bet["bet_id"],))
                    conn.commit()
                    continue
                wallet_ids = sorted([pool_wallet, player_wallet])
                with conn.transaction():
                    rows = conn.execute(
                        "SELECT * FROM wallets WHERE wallet_id = ANY(%s) ORDER BY wallet_id FOR UPDATE",
                        (wallet_ids,),
                    ).fetchall()
                    wallets = {r["wallet_id"]: r for r in rows}
                    before_pool = money(wallets[pool_wallet]["current_balance"])
                    before_player = money(wallets[player_wallet]["current_balance"])
                    refund = min(bet_amount, before_pool)
                    if refund <= 0:
                        conn.execute("UPDATE bets SET status='REFUNDED' WHERE bet_id=%s", (bet["bet_id"],))
                        continue
                    after_pool = money(before_pool - refund)
                    after_player = money(before_player + refund)
                    tx_id = str(uuid.uuid4())
                    conn.execute(
                        """
                        INSERT INTO wallet_transactions(
                            transaction_id, idempotency_key, transaction_type, direction,
                            from_wallet_id, to_wallet_id, initiated_by_user_id, initiated_by_user_type,
                            amount, fee_amount, net_amount,
                            balance_before_from, balance_after_from, balance_before_to, balance_after_to,
                            reference_type, reference_id, status, remarks, completed_at
                        ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'SUCCESS',%s,NOW())
                        """,
                        (
                            tx_id, idem_key, "BET_REFUND", "TRANSFER",
                            pool_wallet, player_wallet, "system", "SYSTEM",
                            float(refund), 0.0, float(refund),
                            float(before_pool), float(after_pool),
                            float(before_player), float(after_player),
                            "GAME_SESSION", sid,
                            "Scheduler restart: bet refunded for interrupted session",
                        ),
                    )
                    conn.execute(
                        "UPDATE wallets SET current_balance=%s, version=version+1, updated_at=NOW() WHERE wallet_id=%s",
                        (float(after_pool), pool_wallet),
                    )
                    conn.execute(
                        "UPDATE wallets SET current_balance=%s, version=version+1, updated_at=NOW() WHERE wallet_id=%s",
                        (float(after_player), player_wallet),
                    )
                    conn.execute("UPDATE bets SET status='REFUNDED' WHERE bet_id=%s", (bet["bet_id"],))
            conn.execute(
                "UPDATE game_sessions SET status='FAILED', completed_at=NOW() WHERE session_id=%s",
                (sid,),
            )
            conn.commit()
    log.info("Interrupted session recovery complete.")


# -------------------------------------------------------------------------
# Main
# -------------------------------------------------------------------------

async def main() -> None:
    init_pool()
    init_db()
    init_redis()

    recover_interrupted_sessions()

    tasks = [asyncio.create_task(run_game_cycle(gk), name=f"cycle-{gk}") for gk in GAME_DEFINITIONS]
    log.info("Game scheduler running for: %s", list(GAME_DEFINITIONS.keys()))
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
