"""Action registry mechanics — register/get/duplicate detection."""
from __future__ import annotations
import pytest

from korder.actions.base import (
    Action,
    all_actions,
    get_action,
    register,
    reset,
    trigger_phrase_map,
)
from korder.actions.parser import invalidate_trigger_cache


@pytest.fixture
def empty_registry():
    """Save/restore the registry around a test that needs a clean slate."""
    saved = list(all_actions())
    reset()
    invalidate_trigger_cache()
    yield
    reset()
    for action in saved:
        register(action)
    invalidate_trigger_cache()


def test_register_and_lookup(empty_registry):
    a = Action(
        name="test_action",
        description="A test",
        triggers={"en": ["test phrase"]},
        op_factory=lambda _: ("key", 99),
    )
    register(a)
    assert get_action("test_action") is a
    assert "test_action" in [x.name for x in all_actions()]


def test_register_duplicate_raises(empty_registry):
    a = Action(name="dup", description="", triggers={}, op_factory=lambda _: ("key", 1))
    register(a)
    with pytest.raises(ValueError, match="already registered"):
        register(a)


def test_trigger_phrase_map_lowercases(empty_registry):
    register(Action(
        name="foo",
        description="",
        triggers={"en": ["Press Foo", "FOO"]},
        op_factory=lambda _: ("key", 1),
    ))
    m = trigger_phrase_map()
    assert m["press foo"] == "foo"
    assert m["foo"] == "foo"


def test_trigger_phrase_collision_raises(empty_registry):
    register(Action(
        name="a1",
        description="",
        triggers={"en": ["same phrase"]},
        op_factory=lambda _: ("key", 1),
    ))
    register(Action(
        name="a2",
        description="",
        triggers={"en": ["same phrase"]},
        op_factory=lambda _: ("key", 2),
    ))
    with pytest.raises(ValueError, match="claimed by both"):
        trigger_phrase_map()


def test_default_registry_has_expected_actions():
    """The package-level registry should be populated by importing korder.actions."""
    import korder.actions  # noqa: F401  (force registration)
    names = {a.name for a in all_actions()}
    expected = {
        "press_enter", "press_tab", "press_escape", "press_backspace",
        "delete_word", "select_all", "undo",
        "select_to_line_start", "select_to_line_end",
        "new_line", "new_paragraph",
    }
    assert expected.issubset(names)
