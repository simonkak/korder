"""Tests for the window-management voice actions. These exercise the
op_factory routing and the args→kwin.* dispatch — the bridge module
itself has its own subprocess-mocked tests in test_kwin.py."""
from __future__ import annotations
from unittest.mock import patch

import korder.actions  # noqa: F401  (self-register)
from korder.actions.base import get_action


# ---- focus_window --------------------------------------------------------


def test_focus_window_dispatches_to_kwin_with_target():
    captured: list[str] = []
    with patch(
        "korder.kwin.activate_window_by_name",
        side_effect=lambda t: captured.append(t) or True,
    ):
        action = get_action("focus_window")
        op = action.op_factory({"target": "Firefox"})
        assert op[0] == "callable"
        op[1]()
    assert captured == ["Firefox"]


def test_focus_window_with_empty_target_returns_pending():
    """No target → factory returns None so MainWindow can mark the
    action pending and wait for the next utterance to fill the param."""
    action = get_action("focus_window")
    assert action.op_factory({}) is None
    assert action.op_factory({"target": ""}) is None
    assert action.op_factory({"target": "   "}) is None


def test_focus_window_strips_whitespace():
    """Common LLM artifact: target = '  Firefox  '. Caller shouldn't
    have to scrub that."""
    captured: list[str] = []
    with patch(
        "korder.kwin.activate_window_by_name",
        side_effect=lambda t: captured.append(t) or True,
    ):
        action = get_action("focus_window")
        _, fn = action.op_factory({"target": "  Firefox  "})
        fn()
    assert captured == ["Firefox"]


# ---- close_window / minimize_window --------------------------------------


def test_close_window_calls_kwin():
    with patch("korder.kwin.close_active_window", return_value=True) as close:
        action = get_action("close_window")
        kind, fn = action.op_factory({})
        assert kind == "callable"
        fn()
    close.assert_called_once_with()


def test_minimize_window_calls_kwin():
    with patch("korder.kwin.minimize_active_window", return_value=True) as minimize:
        action = get_action("minimize_window")
        _, fn = action.op_factory({})
        fn()
    minimize.assert_called_once_with()


# ---- tile_window ---------------------------------------------------------


def test_tile_window_routes_side_to_bridge():
    captured: list[str] = []
    with patch(
        "korder.kwin.tile_active_window",
        side_effect=lambda s: captured.append(s) or True,
    ):
        action = get_action("tile_window")
        for side in ("left", "right", "top", "bottom", "maximize"):
            _, fn = action.op_factory({"side": side})
            fn()
    assert captured == ["left", "right", "top", "bottom", "maximize"]


def test_tile_window_invalid_side_returns_pending():
    """Schema mode constrains side to the enum, but the runtime check
    still defends against anything that slips through (e.g. legacy
    calls or a model without schema mode)."""
    action = get_action("tile_window")
    assert action.op_factory({"side": "diagonal"}) is None
    assert action.op_factory({}) is None


def test_tile_window_handles_uppercase_side():
    """LLM may emit 'Left' instead of 'left' — accept it case-
    insensitively rather than returning pending."""
    captured: list[str] = []
    with patch(
        "korder.kwin.tile_active_window",
        side_effect=lambda s: captured.append(s) or True,
    ):
        action = get_action("tile_window")
        _, fn = action.op_factory({"side": "RIGHT"})
        fn()
    assert captured == ["right"]


# ---- next_desktop / previous_desktop -------------------------------------


def test_next_desktop_calls_bridge():
    with patch("korder.kwin.next_desktop", return_value=True) as nxt:
        action = get_action("next_desktop")
        _, fn = action.op_factory({})
        fn()
    nxt.assert_called_once_with()


def test_previous_desktop_calls_bridge():
    with patch("korder.kwin.previous_desktop", return_value=True) as prv:
        action = get_action("previous_desktop")
        _, fn = action.op_factory({})
        fn()
    prv.assert_called_once_with()


# ---- send_window_to_desktop ---------------------------------------------


def test_send_window_to_desktop_default_direction_is_next():
    captured: list[int] = []
    with patch(
        "korder.kwin.send_active_to_next_desktop",
        side_effect=lambda direction: captured.append(direction) or True,
    ):
        action = get_action("send_window_to_desktop")
        _, fn = action.op_factory({})
        fn()
    assert captured == [+1]


def test_send_window_to_desktop_previous_direction():
    captured: list[int] = []
    with patch(
        "korder.kwin.send_active_to_next_desktop",
        side_effect=lambda direction: captured.append(direction) or True,
    ):
        action = get_action("send_window_to_desktop")
        _, fn = action.op_factory({"direction": "previous"})
        fn()
    assert captured == [-1]


# ---- send_window_to_screen ----------------------------------------------


def test_send_window_to_screen_routes_int():
    captured: list[int] = []
    with patch(
        "korder.kwin.send_active_to_screen",
        side_effect=lambda n: captured.append(n) or True,
    ):
        action = get_action("send_window_to_screen")
        _, fn = action.op_factory({"screen": 2})
        fn()
    assert captured == [2]


def test_send_window_to_screen_accepts_string_int_from_llm():
    """Schema declares integer, but the LLM occasionally emits a
    string. Be tolerant — int(...) parses '2' fine."""
    captured: list[int] = []
    with patch(
        "korder.kwin.send_active_to_screen",
        side_effect=lambda n: captured.append(n) or True,
    ):
        action = get_action("send_window_to_screen")
        _, fn = action.op_factory({"screen": "3"})
        fn()
    assert captured == [3]


def test_send_window_to_screen_invalid_returns_pending():
    action = get_action("send_window_to_screen")
    assert action.op_factory({}) is None
    assert action.op_factory({"screen": "not-a-number"}) is None
    assert action.op_factory({"screen": 0}) is None
    assert action.op_factory({"screen": -1}) is None


# ---- show_overview / show_desktop ---------------------------------------


def test_show_overview_calls_toggle_overview():
    with patch("korder.kwin.toggle_overview", return_value=True) as toggle:
        action = get_action("show_overview")
        _, fn = action.op_factory({})
        fn()
    toggle.assert_called_once_with()


def test_show_desktop_calls_show_desktop_true():
    with patch("korder.kwin.show_desktop", return_value=True) as show:
        action = get_action("show_desktop")
        _, fn = action.op_factory({})
        fn()
    show.assert_called_once_with(True)
