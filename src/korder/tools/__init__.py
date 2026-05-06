"""Tool registry and built-in tool modules.

Importing this package self-registers all built-in tools. Add a new
tool by creating a module that calls register_tool(...) at import time
and adding it to the imports below.

Tools are read-only context providers the LLM can request during a
parse. See ``base.Tool`` for the dataclass shape and ``intent.py`` for
how the iterative loop dispatches them.
"""
from korder.tools import base, audio, bluetooth, mpris, windows  # noqa: F401  (self-register)
from korder.tools.base import (
    Tool,
    all_tools,
    get_tool,
    register_tool,
)

__all__ = [
    "Tool",
    "register_tool",
    "get_tool",
    "all_tools",
]
