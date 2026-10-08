"""Slash-command parsing and client vocabulary helpers."""

from __future__ import annotations
from typing import Any
from stcode.cli.models import APPROVAL_MODES, ApprovalMode


def next_approval_mode(current: str) -> ApprovalMode:
    """The next mode in `APPROVAL_MODES`, wrapping around at the end."""
    if current not in APPROVAL_MODES:
        return "suggest"
    return APPROVAL_MODES[(APPROVAL_MODES.index(current) + 1) % len(APPROVAL_MODES)]  # type: ignore[arg-type]


def parse_approval_mode(value: str) -> ApprovalMode | None:
    """A typed mode name (`/mode auto`) by exact name or unique prefix, else None."""
    normalized = value.strip().lower().replace("_", "-")
    if normalized in APPROVAL_MODES:
        return normalized  # type: ignore[return-value]
    matches = [mode for mode in APPROVAL_MODES if mode.startswith(normalized)]
    return matches[0] if len(matches) == 1 and normalized else None


def parse_command(raw: str) -> tuple[str, str]:
    name, _, argument = raw[1:].partition(" ")
    return name.lower(), argument.strip()


def skill_names(info: dict[str, Any]) -> dict[str, str]:
    """Match case-insensitively while preserving the registry's exact spelling."""
    found: dict[str, str] = {}
    for skill in info.get("skills", []):
        name = str(skill.get("name", ""))
        if name:
            found.setdefault(name.lower(), name)
    return found
