"""The ledger's entry point."""

from src.util import to_cents


def add_entry(book: list[dict], description: str, amount: str) -> dict:
    entry = {"description": description, "cents": to_cents(amount)}
    book.append(entry)
    return entry


def balance(book: list[dict]) -> int:
    return sum(entry["cents"] for entry in book)
