"""Tool registry mechanics — register/get/duplicate detection.

Mirrors tests/test_registry.py for the action registry; the two
registries deliberately use the same shape."""
from __future__ import annotations
import pytest

from korder.tools.base import (
    Tool,
    all_tools,
    get_tool,
    register_tool,
    reset,
)


@pytest.fixture
def empty_registry():
    """Save/restore the registry around tests that need a clean slate."""
    saved = list(all_tools())
    reset()
    yield
    reset()
    for tool in saved:
        register_tool(tool)


def test_register_and_lookup(empty_registry):
    t = Tool(
        name="test_tool",
        description="A test",
        executor=lambda: ["result"],
    )
    register_tool(t)
    assert get_tool("test_tool") is t
    assert "test_tool" in [x.name for x in all_tools()]


def test_register_duplicate_raises(empty_registry):
    t = Tool(name="dup", description="", executor=lambda: [])
    register_tool(t)
    with pytest.raises(ValueError, match="already registered"):
        register_tool(t)


def test_default_args_schema_is_empty(empty_registry):
    """Zero-arg tools — the v1 default — should have an empty
    args_schema dict so the prompt renderer can short-circuit."""
    t = Tool(name="foo", description="", executor=lambda: [])
    register_tool(t)
    assert t.args_schema == {}


def test_executor_receives_kwargs(empty_registry):
    """The executor signature is ``callable(**args)``. Even zero-arg
    tools should be callable with no kwargs; parametric tools should
    receive their args as keyword arguments."""
    captured: dict = {}

    def echo(**kwargs):
        captured.update(kwargs)
        return list(kwargs.items())

    register_tool(Tool(
        name="echo",
        description="Echoes args",
        executor=echo,
        args_schema={"query": {"type": "string"}},
    ))
    tool = get_tool("echo")
    result = tool.executor(query="linkin park")
    assert captured == {"query": "linkin park"}
    assert result == [("query", "linkin park")]


def test_default_registry_has_expected_tools():
    """Importing korder.tools should self-register the v1 catalog."""
    import korder.tools  # noqa: F401  (force registration)
    names = {t.name for t in all_tools()}
    expected = {
        "list_audio_sinks",
        "list_paired_bluetooth_devices",
        "list_active_mpris_players",
    }
    assert expected.issubset(names), f"missing tools: {expected - names}"
