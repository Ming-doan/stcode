"""
Common — vocabulary shared across `core/` subpackages, importing from none of them.

Must stay a leaf. Two tests decide whether something belongs here: it is needed by more
than one subpackage, *and* its logic is not really about any one of them. A type with a
single consumer belongs in that consumer — `OutputStore` lived here until it turned out
only `core/harness/` ever held one.
"""

from stcode.core.common.ids import new_id
from stcode.core.common.paths import (
    CONFIG_PATH_ENV,
    config_dir,
    config_exists,
    default_config_path,
)
from stcode.core.common.tools import ToolDefinition, ToolResult
from stcode.core.common.truncate import DEFAULT_VIEW_LIMIT, NARROW_REQUEST_HINT, elide

__all__ = [
    "CONFIG_PATH_ENV",
    "DEFAULT_VIEW_LIMIT",
    "NARROW_REQUEST_HINT",
    "ToolDefinition",
    "ToolResult",
    "config_dir",
    "config_exists",
    "default_config_path",
    "elide",
    "new_id",
]
