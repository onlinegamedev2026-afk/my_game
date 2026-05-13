from decimal import Decimal, ROUND_DOWN

MONEY_QUANT = Decimal("0.001")


def money(value: str | int | float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_QUANT, rounding=ROUND_DOWN)


def money_str(value: str | int | float | Decimal) -> str:
    return f"{money(value):.3f}"
