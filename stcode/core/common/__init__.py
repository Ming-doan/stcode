"""
Common — vocabulary shared across `core/` subpackages, importing from none of them.

Anything in here must stay a leaf. If a type in `core/common/` ever needs to import
from `core/providers/`, `core/harness/`, or `core/agent/`, it belongs in that package
instead.
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
