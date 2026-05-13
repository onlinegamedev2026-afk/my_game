"""Game orchestrator — v2.

Key changes from v1:
- All game runtime state (phase, session_id, cards_dealt, winner …) lives in
  Redis, not class-level Python dicts.  This makes the app safe to run with
  multiple replicas.
- Bet placement uses SELECT … FOR UPDATE to lock both wallet rows and the
  game_sessions row before writing.
- Game cycle execution is NOT started here — it runs in a separate
  `scheduler/game_scheduler.py` process.  The web app is stateless.
- Idempotency keys protect settlement so it cannot run twice.
"""
import json
import uuid
from decimal import Decimal
from typing import Any

import psycopg

from core.config import settings
from core.redis_client import get_redis, key as rk
from games.andar_bahar import AndarBaharGame
from games.color_guessing import ColorGuessingGame
from games.tin_patti import TinPattiGame
from models.schemas import Actor
from realtime.manager import manager
from services.hierarchy_service import HierarchyService
from transactions.ledger import LedgerService
from utils.money import money


GAME_DEFINITIONS: dict[str, dict[str, Any]] = {
    "tin-patti": {
        "db_key": "TIN_PATTI",
        "title": "Teen Patti",
        "engine": TinPattiGame,
        "total_draws": 6,
        "cards_per_side": 3,
        "has_joker": False,
    },
    "andar-bahar": {
        "db_key": "ANDAR_BAHAR",
        "title": "Andar Bahar",
        "engine": AndarBaharGame,
        "total_draws": None,
        "cards_per_side": None,
        "has_joker": True,
    },
    "color-guessing": {
        "db_key": "COLOR_GUESSING",
        "title": "Color Guess",
        "engine": ColorGuessingGame,
        "total_draws": 1,
        "cards_per_side": None,
        "has_joker": False,
        "is_color_game": True,
    },
}

# Redis key helpers
def _rk(game_key: str, field: str) -> str:
    return rk("game", game_key, field)


class GameOrchestrator:
    """Web-app facing orchestrator — reads Redis state, handles bets."""

    def __init__(self, conn: psycopg.Connection, game_key: str = "tin-patti"):
        if game_key not in GAME_DEFINITIONS:
            raise ValueError("Unknown game.")
        self.conn = conn
        self.game_key = game_key
        self.definition = GAME_DEFINITIONS[game_key]
        self.db_key = self.definition["db_key"]
        self.ledger = LedgerService(conn)

    # ------------------------------------------------------------------
    # Class helpers
    # ------------------------------------------------------------------

    @classmethod
    def available_games(cls) -> list[dict[str, str]]:
        return [
            {"key": k, "title": d["title"], "url": f"/games/{k}"}
            for k, d in GAME_DEFINITIONS.items()
        ]

    # ------------------------------------------------------------------
    # Redis state accessors (async)
    # ------------------------------------------------------------------

    async def _get(self, field: str) -> str | None:
        return await get_redis().get(_rk(self.game_key, field))

    async def _phase(self) -> str:
        return (await self._get("phase")) or "IDLE"

    async def _session_id(self) -> str | None:
        return await self._get("session_id")

    async def _phase_ends_at(self) -> float | None:
        v = await self._get("phase_ends_at")
        return float(v) if v else None

    async def _cards_dealt(self) -> list[dict]:
        v = await self._get("cards_dealt")
        return json.loads(v) if v else []

    async def _winner(self) -> str | None:
        return await self._get("winner")

    async def _joker(self) -> dict | None:
        v = await self._get("joker")
        return json.loads(v) if v else None

    async def _winning_card(self) -> dict | None:
        v = await self._get("winning_card")
        return json.loads(v) if v else None

    def _remaining_seconds(self, phase_ends_at: float | None) -> int:
        import time
        if not phase_ends_at:
            return 0
        return max(0, int(round(phase_ends_at - time.time())))

    @staticmethod
    def _phase_duration_seconds(phase: str) -> int:
        if phase == "BETTING":
            return settings.betting_window_seconds
        if phase == "INITIATING":
            return settings.game_initiation_seconds
        if phase == "SETTLING":
            return settings.after_game_cooldown_seconds
        return 0

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    async def current_state(self, include_totals: bool = False) -> dict:
        phase = await self._phase()
        phase_ends_at = await self._phase_ends_at()
        state = {
            "game_key": self.game_key,
            "session_id": await self._session_id(),
            "phase": phase,
            "phase_ends_at": phase_ends_at,
            "remaining_seconds": self._remaining_seconds(phase_ends_at),
            "phase_duration_seconds": self._phase_duration_seconds(phase),
            "cards_dealt": await self._cards_dealt(),
            "winner": await self._winner(),
            "joker": await self._joker(),
            "winning_card": await self._winning_card(),
            "last_10_winners": self.last_10_winners(),
            "delay": settings.card_drawing_delay_seconds,
            "total_draws": self.definition["total_draws"],
        }
        if include_totals and phase in {"INITIATING", "RUNNING", "SETTLING"}:
            state.update(self.current_totals())
        return state

    def player_bets_for_current_cycle(self, actor: Actor) -> list[dict]:
        session_id = self.conn.execute(
            "SELECT session_id FROM game_sessions WHERE game_key=%s AND status NOT IN ('COMPLETED','FAILED') ORDER BY created_at DESC LIMIT 1",
            (self.db_key,),
        ).fetchone()
        if not session_id:
            return []
        rows = self.conn.execute(
            """
            SELECT side, amount, status, created_at FROM bets
            WHERE session_id=%s AND player_id=%s ORDER BY created_at ASC
            """,
            (session_id["session_id"], actor.id),
        ).fetchall()
        return [
            {"side": r["side"], "amount": str(r["amount"]), "status": r["status"], "created_at": str(r["created_at"])}
            for r in rows
        ]

    def current_totals(self) -> dict[str, str]:
        row = self.conn.execute(
            "SELECT group_a_total, group_b_total FROM game_sessions WHERE game_key=%s AND status NOT IN ('COMPLETED','FAILED') ORDER BY created_at DESC LIMIT 1",
            (self.db_key,),
        ).fetchone()
        if not row:
            return {"group_a_total": "0.000", "group_b_total": "0.000"}
        return {"group_a_total": f"{row['group_a_total']:.3f}", "group_b_total": f"{row['group_b_total']:.3f}"}

    def last_10_winners(self) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT winner FROM game_sessions
            WHERE game_key=%s AND status='COMPLETED' AND winner IS NOT NULL
            ORDER BY completed_at DESC LIMIT 10
            """,
            (self.db_key,),
        ).fetchall()
        return [row["winner"] for row in reversed(rows)]

    def active_game_for_player(self, actor: Actor) -> dict | None:
        row = self.conn.execute(
            """
            SELECT gs.game_key, gs.status
            FROM bets b JOIN game_sessions gs ON gs.session_id=b.session_id
            WHERE b.player_id=%s AND b.status='PLACED' AND gs.status IN ('BETTING','INITIATING','RUNNING','SETTLING')
            ORDER BY gs.created_at DESC LIMIT 1
            """,
            (actor.id,),
        ).fetchone()
        if not row:
            return None
        return {
            "game_key": self._route_key(row["game_key"]),
            "title": self._title_for_db_key(row["game_key"]),
            "status": row["status"],
        }

    # ------------------------------------------------------------------
    # Bet placement (web app — PostgreSQL row-level locking)
    # ------------------------------------------------------------------

    async def place_bet(self, actor: Actor, side: str, amount: Decimal) -> None:
        if side not in {"A", "B"}:
            raise ValueError("Choose side A or B.")
        amount = money(amount)
        if amount < settings.min_bet:
            raise ValueError("Minimum bet is 10.000.")

        # Fast check: phase must be BETTING in Redis
        if await self._phase() != "BETTING":
            raise ValueError("Bets are accepted only during the active betting time.")

        active = self.active_game_for_player(actor)
        if active and active["game_key"] != self.game_key:
            raise ValueError(f"You already have an active {active['title']} round.")

        bet_id = str(uuid.uuid4())
        tx_id = str(uuid.uuid4())
        pool_wallet = self._pool_wallet()
        column = "group_a_total" if side == "A" else "group_b_total"

        with self.conn.transaction():
            # Lock the game session row to confirm betting is still open in DB
            session = self.conn.execute(
                """
                SELECT session_id, status FROM game_sessions
                WHERE game_key=%s AND status='BETTING'
                ORDER BY created_at DESC LIMIT 1
                FOR UPDATE
                """,
                (self.db_key,),
            ).fetchone()
            if not session:
                raise ValueError("Bets are accepted only during the active betting time.")
            session_id = session["session_id"]

            # Lock both wallets in alphabetical order to prevent deadlocks
            wallet_ids = sorted([actor.wallet_id, pool_wallet])
            rows = self.conn.execute(
                "SELECT * FROM wallets WHERE wallet_id = ANY(%s) ORDER BY wallet_id FOR UPDATE",
                (wallet_ids,),
            ).fetchall()
            wallets = {r["wallet_id"]: r for r in rows}
            from_wallet = wallets[actor.wallet_id]
            to_wallet = wallets[pool_wallet]

            if from_wallet["status"] != "ACTIVE" or to_wallet["status"] != "ACTIVE":
                raise ValueError("Wallets must be active.")

            before_from = money(from_wallet["current_balance"])
            before_to = money(to_wallet["current_balance"])
            if before_from < amount:
                raise ValueError(
                    f"Not enough credit. Available: {before_from:.3f}, requested: {amount:.3f}."
                )
            after_from = money(before_from - amount)
            after_to = money(before_to + amount)

            self.conn.execute(
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
                    tx_id, f"bet:{bet_id}", "BET_DEBIT", "TRANSFER",
                    actor.wallet_id, pool_wallet,
                    actor.id, actor.role,
                    float(amount), 0.0, float(amount),
                    float(before_from), float(after_from),
                    float(before_to), float(after_to),
                    "BET", bet_id,
                ),
            )
            self.conn.execute(
                "UPDATE wallets SET current_balance=%s, version=version+1, updated_at=NOW() WHERE wallet_id=%s",
                (float(after_from), actor.wallet_id),
            )
            self.conn.execute(
                "UPDATE wallets SET current_balance=%s, version=version+1, updated_at=NOW() WHERE wallet_id=%s",
                (float(after_to), pool_wallet),
            )
            self.conn.execute(
                "INSERT INTO bets(bet_id, session_id, player_id, side, amount) VALUES(%s,%s,%s,%s,%s)",
                (bet_id, session_id, actor.id, side, float(amount)),
            )
            self.conn.execute(
                f"UPDATE game_sessions SET {column}={column}+%s WHERE session_id=%s",
                (float(amount), session_id),
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _pool_wallet(self) -> str:
        row = self.conn.execute(
            "SELECT w.wallet_id FROM accounts a JOIN wallets w ON w.owner_id=a.id WHERE a.username='system_pool'"
        ).fetchone()
        return row["wallet_id"]

    @staticmethod
    def _route_key(db_key: str) -> str:
        for k, d in GAME_DEFINITIONS.items():
            if d["db_key"] == db_key:
                return k
        return db_key.lower().replace("_", "-")

    @staticmethod
    def _title_for_db_key(db_key: str) -> str:
        for d in GAME_DEFINITIONS.values():
            if d["db_key"] == db_key:
                return d["title"]
        return db_key

    @staticmethod
    def _card_dict(card: tuple[str, str] | None) -> dict | None:
        if not card:
            return None
        return {"rank": card[0], "suit": card[1]}
