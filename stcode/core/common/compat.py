"""
Compat — the two standard-library names stcode uses that Python 3.10 lacks.

Both arrived in 3.11. Everything else in the codebase already runs on 3.10, so these
two are the entire cost of supporting it; import them from here, never from the
standard library directly.
"""

from __future__ import annotations

import sys
from enum import Enum

if sys.version_info >= (3, 11):
    import tomllib
    from enum import StrEnum
else:  # pragma: no cover - exercised by the 3.10 CI job
    import tomli as tomllib

    class StrEnum(str, Enum):
        """`enum.StrEnum` for 3.10: members are strings and print as their value."""

        def __str__(self) -> str:
            return str(self.value)


__all__ = ["StrEnum", "tomllib"]
