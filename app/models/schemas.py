from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Actor:
    id: str
    username: str
    display_name: str
    email: str | None
    role: str
    status: str
    parent_id: str | None
    wallet_id: str
    balance: Decimal
