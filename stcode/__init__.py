"""
stcode — Terminal Coding Agent
"""

import importlib.metadata

try:
    __version__ = importlib.metadata.version("stcode")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.0.0"