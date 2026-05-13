import datetime as dt
import secrets
import string
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

try:
    from dev_time_utils.classes import Person
except Exception:
    Person = None


def generate_account_id(full_name: str) -> str:
    if Person is not None:
        return Person.generate_id(full_name)
    sanitized_name = full_name.strip().replace(" ", "").lower()
    return f"{sanitized_name}@{dt.datetime.now().strftime('%S%f')[:5]}#{secrets.randbelow(9000) + 1000}"


def generate_password() -> str:
    now = dt.datetime.now().isoformat(timespec="microseconds")
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    length = 8 + (sum(ord(ch) for ch in now) % 3)
    password = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*"),
    ]
    password.extend(secrets.choice(alphabet) for _ in range(length - len(password)))
    secrets.SystemRandom().shuffle(password)
    return "".join(password)
