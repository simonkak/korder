"""Worker-thread filtering logic — verifies that write_mode toggles flip
state correctly and gate text/char ops accordingly while letting action
ops (key/combo/subprocess) execute regardless. Tests the pure logic
without spinning up a QThread or QApplication."""
from __future__ import annotations

import korder.actions  # noqa: F401  (default registrations)
from korder.actions.codes import KEY_ENTER


def _filter(ops: list[tuple], initial_mode: bool) -> tuple[list[tuple], bool]:
    """Mirrors _InjectWorker.run's filter pass without the Qt machinery —
    walks ops, tracks mode, returns (filtered_ops, final_mode)."""
    filtered: list[tuple] = []
    mode = initial_mode
    for op in ops:
        kind = op[0]
        if kind == "write_mode":
            mode = bool(op[1])
        elif kind in ("text", "char"):
            if mode:
                filtered.append(op)
        else:
            filtered.append(op)
    return filtered, mode


def test_text_op_gated_when_write_mode_off():
    ops = [("text", "hello")]
    filtered, mode = _filter(ops, initial_mode=False)
    assert filtered == []
    assert mode is False


def test_text_op_passes_when_write_mode_on():
    ops = [("text", "hello")]
    filtered, mode = _filter(ops, initial_mode=True)
    assert filtered == [("text", "hello")]
    assert mode is True


def test_pisz_then_text_types():
    """Pisz hello — turn on, then text passes."""
    ops = [("write_mode", True), ("text", "hello")]
    filtered, mode = _filter(ops, initial_mode=False)
    assert filtered == [("text", "hello")]
    assert mode is True


def test_text_then_przestań_then_more_text():
    """First text passes (mode on), Przestań flips off, second text dropped."""
    ops = [
        ("text", "first"),
        ("write_mode", False),
        ("text", "second"),
    ]
    filtered, mode = _filter(ops, initial_mode=True)
    assert filtered == [("text", "first")]
    assert mode is False


def test_action_ops_execute_regardless_of_mode():
    """Keys, combos, media should ALWAYS run — write_mode only gates text."""
    ops = [
        ("key", KEY_ENTER),
        ("combo", [29, 14]),
        ("subprocess", ["wpctl"]),
    ]
    filtered, _mode = _filter(ops, initial_mode=False)
    assert filtered == ops  # all kept, even though write_mode is off


def test_mixed_dictation_with_inline_toggle():
    """Pisz hello Przestań press enter — types hello, then presses Enter
    even though write mode is now off."""
    ops = [
        ("write_mode", True),
        ("text", "hello"),
        ("write_mode", False),
        ("key", KEY_ENTER),
    ]
    filtered, mode = _filter(ops, initial_mode=False)
    assert filtered == [("text", "hello"), ("key", KEY_ENTER)]
    assert mode is False
