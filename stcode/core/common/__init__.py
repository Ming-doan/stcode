"""
Common — vocabulary shared across `core/` subpackages, importing from none of them.

Anything in here must stay a leaf. If a type in `core/common/` ever needs to import
from `core/providers/`, `core/harness/`, or `core/agent/`, it belongs in that package
instead.
"""

from stcode.core.common.tools import ToolDefinition, ToolResult

__all__ = ["ToolDefinition", "ToolResult"]
