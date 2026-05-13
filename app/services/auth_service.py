import psycopg

from core.security import sign_session, verify_password
from models.schemas import Actor
from utils.money import money


def actor_from_row(row: dict) -> Actor:
    return Actor(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        email=row["email"],
        role=row["role"],
        status=row["status"],
        parent_id=row["parent_id"],
        wallet_id=row["wallet_id"],
        balance=money(row["current_balance"]),
    )


class AuthService:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def login(self, username: str, password: str, expected_role: str) -> str | None:
        actor = self.verify_credentials(username, password, expected_role)
        return sign_session(actor.id, actor.role) if actor else None

    def verify_credentials(self, username: str, password: str, expected_role: str) -> Actor | None:
        row = self._credential_row(username, expected_role)
        if not row or row["status"] != "ACTIVE":
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        return actor_from_row(row)

    def credential_failure_reason(self, username: str, password: str, expected_role: str) -> str:
        row = self._credential_row(username, expected_role)
        if row and row["status"] != "ACTIVE" and verify_password(password, row["password_hash"]):
            return "inactive"
        return "invalid"

    def _credential_row(self, username: str, expected_role: str):
        return self.conn.execute(
            """
            SELECT a.*, w.wallet_id, w.current_balance
            FROM accounts a JOIN wallets w ON w.owner_id = a.id
            WHERE a.username=%s AND a.role=%s
            """,
            (username, expected_role),
        ).fetchone()

    def get_actor(self, user_id: str) -> Actor | None:
        row = self.conn.execute(
            """
            SELECT a.*, w.wallet_id, w.current_balance
            FROM accounts a JOIN wallets w ON w.owner_id = a.id
            WHERE a.id=%s
            """,
            (user_id,),
        ).fetchone()
        return actor_from_row(row) if row else None
