"""Tool dataclass + module-level registry.

Tools are the read-only context-providers the intent loop offers to
the LLM. The LLM iterates: emit ``tool_calls`` on a turn, Korder runs
each, results are appended to the conversation, the LLM continues
until it emits ``actions`` (dispatch) or ``response`` (chat). Same
mechanic as MCP / OpenAI function-calling — the loop is bounded but
otherwise the LLM decides when it has enough information to act.

Distinct from Action: an Action MUTATES system state (presses a key,
switches a sink, locks the screen); a Tool RETURNS data. Same registry
shape — name + description + a single callable — kept as similar to
``actions/base.py`` as practical so both registries are familiar to
read together.

Mirrors opencode's tool-as-first-class shape: ``{name, description,
args, execute}``. v1 ships zero-arg tools (state enumerators) but
``args_schema`` exists from day one so the first parametric tool —
``search_spotify(query)``, ``screenshot_window(name)`` — needs no
migration.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable

ToolResult = list | dict | str


@dataclass(frozen=True)
class Tool:
    """A registered context-provider tool.

    name: stable identifier (snake_case) — referenced from
          ``Action.tools`` and used as the key for tool_calls /
          tool-result messages in the LLM conversation.
    description: short English string shown to the LLM. Should explain
          WHAT the tool returns and HOW the LLM should use it (which
          action's params it disambiguates). Same audience as
          ``Action.description``.
    executor: callable invoked as ``executor(**args)`` where args is
          the dict the LLM emitted in tool_calls. Returns a JSON-
          serializable result. v1 tools take no args; the kwargs
          signature is for forward compatibility.
    args_schema: JSON-Schema "properties" dict describing the tool's
          parameters. Empty dict (default) means zero-arg. Format
          matches ``Action.parameters`` so the same renderer can
          format both surfaces in the LLM prompt.
    """
    name: str
    description: str
    executor: Callable[..., ToolResult]
    args_schema: dict[str, Any] = field(default_factory=dict)


_REGISTRY: dict[str, Tool] = {}


def register_tool(tool: Tool) -> Tool:
    if tool.name in _REGISTRY:
        raise ValueError(f"tool {tool.name!r} already registered")
    _REGISTRY[tool.name] = tool
    return tool


def get_tool(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def all_tools() -> list[Tool]:
    return list(_REGISTRY.values())


def reset() -> None:
    """Clears the registry. For tests only."""
    _REGISTRY.clear()
