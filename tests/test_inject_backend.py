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


def test_volume_up_routes_through_wpctl(fake_backend):
    """Volume actions go through wpctl directly so they can't race with
    the ducker's wpctl writes (an old keycode path lost increments)."""
    backend, calls = fake_backend
    backend.type("głośniej")
    wpctl_call = next(c for c in calls if c[0] == "wpctl")
    assert wpctl_call == [
        "wpctl",
        "set-volume",
        "@DEFAULT_AUDIO_SINK@",
        "5%+",
    ]


def test_volume_op_with_explicit_step(fake_backend):
    """system_volume payload of (direction, step_pct) lets the LLM bump
    the wpctl step beyond the 5% default for 'znacznie głośniej' /
    'louder by 20%' utterances."""
    backend, _ = fake_backend
    calls: list[list[str]] = []
    def capture(cmd, *a, **kw):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()
    from korder import inject as inject_mod
    backend_module_run = inject_mod.subprocess.run
    inject_mod.subprocess.run = capture
    try:
        backend.execute_ops([("system_volume", ("up", 20))])
        backend.execute_ops([("system_volume", ("down", 2))])
        backend.execute_ops([("system_volume", ("mute_toggle", 0))])
    finally:
        inject_mod.subprocess.run = backend_module_run
    assert calls[0] == ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "20%+"]
    assert calls[1] == ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "2%-"]
    assert calls[2] == ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"]


def test_play_music_emits_play_pause_keycode(fake_backend):
    backend, calls = fake_backend
    backend.type("play music")
    # KEY_PLAYPAUSE = 164
    key_call = next(c for c in calls if c[0] == "ydotool" and c[1] == "key")
    assert "164:1" in key_call and "164:0" in key_call


def test_subprocess_op_swallows_failure(fake_backend, monkeypatch):
    """The subprocess op kind stays for future actions (screenshot, app launch);
    failures should not raise."""
    from korder import inject
    from korder.actions.base import Action, register, reset
    from korder.actions.parser import invalidate_trigger_cache
    import korder.actions  # ensure default registrations
    from korder.actions.base import all_actions

    backend, _calls = fake_backend

    # Save and replace registry so we can register a test subprocess action.
    saved = list(all_actions())
    try:
        reset()
        for a in saved:
            register(a)
        register(Action(
            name="test_failing_subproc",
            description="",
            triggers={"en": ["fail subproc"]},
            op_factory=lambda _: ("subprocess", ["false"]),
        ))
        invalidate_trigger_cache()

        class FailingResult:
            returncode = 1
            stdout = ""
            stderr = ""

        def fake_run(cmd, *args, **kwargs):
            if cmd == ["false"]:
                return FailingResult()
            class OK:
                returncode = 0
                stdout = ""
                stderr = ""
            return OK()

        monkeypatch.setattr(inject.subprocess, "run", fake_run)
        # Should not raise
        backend.type("fail subproc")
    finally:
        reset()
        for a in saved:
            register(a)
        invalidate_trigger_cache()
