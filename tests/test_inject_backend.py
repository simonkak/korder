"""Backend integration test — verifies YdotoolBackend serializes work
correctly and translates op tuples into the right subprocess calls.
The actual ydotool binary is mocked so the test runs without uinput
permissions or a Wayland session."""
from __future__ import annotations
from unittest.mock import patch

import pytest

import korder.actions  # noqa: F401
from korder.actions.codes import KEY_BACKSPACE, KEY_ENTER, KEY_LCTRL


@pytest.fixture
def fake_backend(monkeypatch):
    """Construct a YdotoolBackend that doesn't shell out — every subprocess
    call is recorded into a list for assertion."""
    from korder import inject

    # Pretend ydotool is on PATH so __init__ succeeds.
    monkeypatch.setattr(inject.shutil, "which", lambda name: f"/usr/bin/{name}")

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""
        return Result()

    monkeypatch.setattr(inject.subprocess, "run", fake_run)
    monkeypatch.setattr(inject.time, "sleep", lambda _s: None)

    backend = inject.YdotoolBackend()
    return backend, calls


def test_press_enter_translates_to_ydotool_key(fake_backend):
    backend, calls = fake_backend
    backend.type("press enter")
    # First and only call: ydotool key 28:1 28:0
    assert any(
        c[0] == "ydotool" and c[1] == "key" and "28:1" in c and "28:0" in c
        for c in calls
    )


def test_combo_emits_modifier_then_key_with_reverse_release(fake_backend):
    backend, calls = fake_backend
    backend.type("usuń słowo")
    # Expected sequence: 29:1 14:1 14:0 29:0
    key_call = next(c for c in calls if c[0] == "ydotool" and c[1] == "key")
    assert key_call[2:] == [
        f"{KEY_LCTRL}:1",
        f"{KEY_BACKSPACE}:1",
        f"{KEY_BACKSPACE}:0",
        f"{KEY_LCTRL}:0",
    ]


def test_text_with_diacritics_routes_through_paste(fake_backend, monkeypatch):
    """Non-ASCII text should go via wl-copy + Ctrl+V."""
    backend, calls = fake_backend
    # Pretend wl-copy is available
    backend._has_wl_copy = True
    backend.type("Cześć")
    # Expect one wl-copy call, then a ydotool key for Ctrl+V
    cmds = [c[0] for c in calls]
    assert "wl-copy" in cmds
    paste_key_calls = [c for c in calls if c[0] == "ydotool" and c[1] == "key"]
    assert paste_key_calls
    # Ctrl+V combo:
    assert paste_key_calls[0][2:] == [
        f"{KEY_LCTRL}:1",
        "47:1",  # KEY_V
        "47:0",
        f"{KEY_LCTRL}:0",
    ]


def test_pure_ascii_does_not_paste(fake_backend):
    """ASCII text should be typed directly without touching the clipboard."""
    backend, calls = fake_backend
    backend._has_wl_copy = True
    backend.type("hello world")
    cmds = [c[0] for c in calls]
    assert "wl-copy" not in cmds
    assert any(c[0] == "ydotool" and c[1] == "type" for c in calls)


def test_empty_text_is_noop(fake_backend):
    backend, calls = fake_backend
    backend.type("")
    assert calls == []
