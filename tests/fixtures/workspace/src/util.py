"""Money helpers. Integer cents, never floats."""

RETRY_LIMIT = 3


def to_cents(amount: str) -> int:
    whole, _, fraction = amount.partition(".")
    return int(whole) * 100 + int((fraction + "00")[:2])


def format_cents(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"
