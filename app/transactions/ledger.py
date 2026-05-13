"""Ledger service — all wallet transfers go through here.

Key improvement over v1: uses SELECT ... FOR UPDATE to lock both wallet rows
inside a single PostgreSQL transaction, preventing race conditions on
concurrent transfers.
"""
import uuid
from decimal import Decimal

import psycopg

from models.schemas import Actor
from utils.money import money


class LedgerService:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def transfer(
        self,
        *,
        actor: Actor,
        from_wallet_id: str,
        to_wallet_id: str,
        amount: Decimal,
        transaction_type: str,
        idempotency_key: str,
        reference_type: str | None = None,
        reference_id: str | None = None,
        fee_amount: Decimal = Decimal("0.000"),
        remarks: str | None = None,
    ) -> str:
        amount = money(amount)
        fee_amount = money(fee_amount)
        net_amount = money(amount)
        if amount <= 0:
            raise ValueError("Amount must be positive.")

        # Idempotency check — outside the transaction so we can return early fast.
        existing = self.conn.execute(
            "SELECT transaction_id FROM wallet_transactions WHERE idempotency_key=%s AND status='SUCCESS'",
            (idempotency_key,),
        ).fetchone()
        if existing:
            return existing["transaction_id"]

        tx_id = str(uuid.uuid4())

        # Lock wallets in a consistent order (alphabetical by wallet_id) to
        # avoid deadlocks when two transfers involve the same pair of wallets.
        ids_ordered = sorted([from_wallet_id, to_wallet_id])
        with self.conn.transaction():
            rows = self.conn.execute(
                "SELECT * FROM wallets WHERE wallet_id = ANY(%s) ORDER BY wallet_id FOR UPDATE",
                (ids_ordered,),
            ).fetchall()
            wallets = {row["wallet_id"]: row for row in rows}

            from_wallet = wallets[from_wallet_id]
            to_wallet = wallets[to_wallet_id]

            if from_wallet["status"] != "ACTIVE" or to_wallet["status"] != "ACTIVE":
                raise ValueError("Wallets must be active.")

            before_from = money(from_wallet["current_balance"])
            before_to = money(to_wallet["current_balance"])

            if before_from < amount:
                raise ValueError(
                    "This account does not have enough credit. "
                    f"Available balance: {before_from:.3f}, requested amount: {amount:.3f}."
                )

            after_from = money(before_from - amount)
            after_to = money(before_to + net_amount)

            self.conn.execute(
                """
                INSERT INTO wallet_transactions(
                    transaction_id, idempotency_key, transaction_type, direction,
                    from_wallet_id, to_wallet_id, initiated_by_user_id, initiated_by_user_type,
                    amount, fee_amount, net_amount, balance_before_from, balance_after_from,
                    balance_before_to, balance_after_to, reference_type, reference_id,
                    status, remarks, completed_at
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'SUCCESS',%s,NOW())
                """,
                (
                    tx_id, idempotency_key, transaction_type, "TRANSFER",
                    from_wallet_id, to_wallet_id,
                    actor.id, actor.role,
                    float(amount), float(fee_amount), float(net_amount),
                    float(before_from), float(after_from),
                    float(before_to), float(after_to),
                    reference_type, reference_id, remarks,
                ),
            )
            self.conn.execute(
                "UPDATE wallets SET current_balance=%s, version=version+1, updated_at=NOW() WHERE wallet_id=%s",
                (float(after_from), from_wallet_id),
            )
            self.conn.execute(
                "UPDATE wallets SET current_balance=%s, version=version+1, updated_at=NOW() WHERE wallet_id=%s",
                (float(after_to), to_wallet_id),
            )

        return tx_id
