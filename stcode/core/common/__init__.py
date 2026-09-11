"""
Common — vocabulary shared across `core/` subpackages, importing from none of them.

Must stay a leaf. If a type here ever needs `core/providers/`, `core/harness/` or
`core/agent/`, it belongs in that package instead.
"""

from stcode.core.common.tools import ToolDefinition, ToolResult
from stcode.core.common.truncate import DEFAULT_VIEW_LIMIT, NARROW_REQUEST_HINT, elide

__all__ = [
    "DEFAULT_VIEW_LIMIT",
    "NARROW_REQUEST_HINT",
    "ToolDefinition",
    "ToolResult",
    "elide",
]
