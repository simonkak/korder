"""Tests for the KWin scripting bridge. Mocks subprocess.run so the
tests exercise the qdbus argument shape and the JS-body construction
without depending on a live KWin / D-Bus session."""
from __future__ import annotations
import json
import subprocess
from unittest.mock import patch

from korder import kwin


def _ok(stdout: str = "0\n"):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail(*_args, **_kwargs):
    raise subprocess.CalledProcessError(returncode=1, cmd=["qdbus6"], output="", stderr="error")


# ---- direct /KWin operations --------------------------------------------


def test_next_desktop_calls_kwin_dbus():
    calls: list[list[str]] = []
    with patch("korder.kwin.subprocess.run", side_effect=lambda c, *a, **k: calls.append(c) or _ok()):
        assert kwin.next_desktop() is True
    assert calls[0][:2] == ["qdbus6", "org.kde.KWin"]
    assert "nextDesktop" in calls[0][-1]


def test_set_current_desktop_passes_int():
    calls: list[list[str]] = []
    with patch("korder.kwin.subprocess.run", side_effect=lambda c, *a, **k: calls.append(c) or _ok()):
        assert kwin.set_current_desktop(3) is True
    assert calls[0][-1] == "3"
    assert "setCurrentDesktop" in calls[0][-2]


def test_show_desktop_serializes_bool():
    calls: list[list[str]] = []
    with patch("korder.kwin.subprocess.run", side_effect=lambda c, *a, **k: calls.append(c) or _ok()):
        assert kwin.show_desktop(True) is True
        assert kwin.show_desktop(False) is True
    assert calls[0][-1] == "true"
    assert calls[1][-1] == "false"


def test_qdbus_failure_returns_false():
    with patch("korder.kwin.subprocess.run", side_effect=_fail):
        assert kwin.next_desktop() is False
    with patch("korder.kwin.subprocess.run", side_effect=FileNotFoundError):
        assert kwin.next_desktop() is False


# ---- /Effects ------------------------------------------------------------


def test_toggle_overview_hits_effects_dbus():
    calls: list[list[str]] = []
    with patch("korder.kwin.subprocess.run", side_effect=lambda c, *a, **k: calls.append(c) or _ok()):
        assert kwin.toggle_overview() is True
    assert calls[0][2] == "/Effects"
    assert "toggleEffect" in calls[0][3]
    assert calls[0][-1] == "overview"


def test_toggle_windowview_uses_correct_effect_name():
    calls: list[list[str]] = []
    with patch("korder.kwin.subprocess.run", side_effect=lambda c, *a, **k: calls.append(c) or _ok()):
        kwin.toggle_windowview()
    assert calls[0][-1] == "windowview"


# ---- /Scripting infrastructure ------------------------------------------


def test_run_script_loads_then_starts_then_unloads():
    """Three D-Bus calls in order: loadScript, start, unloadScript.
    The temp file path appears as the first arg of load and the last
    arg of unload (KWin records the path as the plugin name when no
    explicit name was supplied)."""
    calls: list[list[str]] = []

    def fake_run(c, *a, **k):
        calls.append(c)
        return _ok()

    with patch("korder.kwin.subprocess.run", side_effect=fake_run):
        kwin.close_active_window()

    # First call: loadScript with a /tmp/...js path
    assert "loadScript" in calls[0][-2]
    script_path = calls[0][-1]
    assert script_path.startswith("/tmp/") and script_path.endswith(".js")
    # Second call: start
    assert "start" in calls[1][-1]
    # Third call: unloadScript with the same path
    assert "unloadScript" in calls[2][-2]
    assert calls[2][-1] == script_path


def test_run_script_returns_false_when_load_fails():
    with patch("korder.kwin.subprocess.run", side_effect=_fail):
        assert kwin.close_active_window() is False


def test_run_script_cleans_up_tempfile_even_on_failure():
    """If the qdbus call raises, the temp .js file we wrote must
    still get removed — we don't want /tmp filling up with korder
    droppings on a misconfigured KWin."""
    paths_seen: list[str] = []

    def fake_run(c, *a, **k):
        # Capture the path before failing so we can verify cleanup.
        if len(c) >= 5 and "loadScript" in c[3]:
            paths_seen.append(c[-1])
        raise subprocess.CalledProcessError(returncode=1, cmd=c)

    with patch("korder.kwin.subprocess.run", side_effect=fake_run):
        kwin.close_active_window()

    import os
    for p in paths_seen:
        assert not os.path.exists(p), f"{p} should have been cleaned up"


# ---- Script body shape (per-window operations) --------------------------


def _capture_script_body() -> str:
    """Run a fake qdbus that reads back the JS body Korder wrote."""
    captured = {"body": ""}

    def fake_run(c, *a, **k):
        # On loadScript, read the file — that's the JS we want to inspect.
        if len(c) >= 5 and "loadScript" in c[3]:
            with open(c[-1]) as f:
                captured["body"] = f.read()
        return _ok()

    return captured, fake_run


def test_close_active_window_emits_close_call_in_js():
    captured, fake_run = _capture_script_body()
    with patch("korder.kwin.subprocess.run", side_effect=fake_run):
        kwin.close_active_window()
    assert "closeWindow" in captured["body"]
    assert "workspace.activeWindow" in captured["body"]


def test_minimize_active_window_sets_minimized_true():
    captured, fake_run = _capture_script_body()
    with patch("korder.kwin.subprocess.run", side_effect=fake_run):
        kwin.minimize_active_window()
    assert "minimized = true" in captured["body"]


def test_tile_active_window_uses_correct_slot_per_side():
    for side, expected_slot in [
        ("left", "slotWindowQuickTileLeft"),
        ("right", "slotWindowQuickTileRight"),
        ("top", "slotWindowQuickTileTop"),
        ("bottom", "slotWindowQuickTileBottom"),
    ]:
        captured, fake_run = _capture_script_body()
        with patch("korder.kwin.subprocess.run", side_effect=fake_run):
            kwin.tile_active_window(side)
        assert expected_slot in captured["body"], (
            f"{side} should map to {expected_slot}"
        )


def test_tile_active_window_maximize_uses_slot_window_maximize():
    captured, fake_run = _capture_script_body()
    with patch("korder.kwin.subprocess.run", side_effect=fake_run):
        kwin.tile_active_window("maximize")
    assert "slotWindowMaximize" in captured["body"]


def test_tile_active_window_rejects_invalid_side():
    """No script load attempt for nonsense input — refuse early."""
    with patch("korder.kwin.subprocess.run", side_effect=lambda *a, **k: _ok()) as run:
        assert kwin.tile_active_window("diagonal") is False
        run.assert_not_called()


def test_send_active_to_desktop_embeds_one_indexed_value():
    """User says 'desktop 2'; KWin desktops are zero-indexed in the
    array so the script subtracts 1."""
    captured, fake_run = _capture_script_body()
    with patch("korder.kwin.subprocess.run", side_effect=fake_run):
        kwin.send_active_to_desktop(2)
    assert "const idx = 2 - 1;" in captured["body"]


def test_send_active_to_next_desktop_follows_window():
    """The 'send to next desktop' UX moves the user along with the
    window — otherwise they're staring at empty wallpaper. Body
    must include both the desktop assignment and currentDesktop set."""
    captured, fake_run = _capture_script_body()
    with patch("korder.kwin.subprocess.run", side_effect=fake_run):
        kwin.send_active_to_next_desktop(direction=+1)
    body = captured["body"]
    assert "w.desktops = [target]" in body
    assert "workspace.currentDesktop = target" in body


def test_activate_window_by_name_embeds_target_safely():
    """Target string goes through json.dumps so embedded quotes /
    backslashes can't escape into the JS body."""
    captured, fake_run = _capture_script_body()
    target = 'Firefox "How I Chose" \\path'
    with patch("korder.kwin.subprocess.run", side_effect=fake_run):
        kwin.activate_window_by_name(target)
    body = captured["body"]
    # The target appears as a JSON-encoded string literal
    expected_literal = json.dumps(target)
    assert expected_literal in body


def test_activate_window_by_name_empty_target_is_noop():
    """An empty target after stripping whitespace must not run any
    KWin script — there's nothing to match against and we'd risk
    activating whatever the script's tie-break picks."""
    with patch("korder.kwin.subprocess.run", side_effect=lambda *a, **k: _ok()) as run:
        assert kwin.activate_window_by_name("") is False
        assert kwin.activate_window_by_name("   ") is False
        run.assert_not_called()


def test_activate_window_by_name_uses_token_overlap_score():
    """Smoke check on the matching algorithm: the JS body must
    enumerate windowList and compare token sets, not do a substring
    match (which would let 'Firefox' incorrectly score against 'Box
    Fire' or similar)."""
    captured, fake_run = _capture_script_body()
    with patch("korder.kwin.subprocess.run", side_effect=fake_run):
        kwin.activate_window_by_name("kate")
    body = captured["body"]
    assert "workspace.windowList()" in body
    assert "haystackTokens" in body
    assert "workspace.activeWindow = best" in body


def test_minimize_window_by_name_runs_minimize_action():
    """Same fuzzy matcher as activate, different action — the matched
    window's `minimized` property is set to true."""
    captured, fake_run = _capture_script_body()
    with patch("korder.kwin.subprocess.run", side_effect=fake_run):
        kwin.minimize_window_by_name("Firefox")
    body = captured["body"]
    assert "workspace.windowList()" in body
    assert "best.minimized = true" in body
    # The activate dispatch must NOT appear when the verb is minimize.
    assert "workspace.activeWindow = best" not in body


def test_close_window_by_name_runs_close_action():
    """The matched window's `closeWindow()` method is called (Plasma
    6 KWin scripting API)."""
    captured, fake_run = _capture_script_body()
    with patch("korder.kwin.subprocess.run", side_effect=fake_run):
        kwin.close_window_by_name("Firefox")
    body = captured["body"]
    assert "best.closeWindow();" in body
    assert "workspace.activeWindow = best" not in body
    assert "best.minimized = true" not in body


def test_minimize_close_by_name_empty_target_is_noop():
    """Empty target → no script runs. Caller routes to the
    active-window helper instead, never down this path."""
    with patch("korder.kwin.subprocess.run", side_effect=lambda *a, **k: _ok()) as run:
        assert kwin.minimize_window_by_name("") is False
        assert kwin.minimize_window_by_name("   ") is False
        assert kwin.close_window_by_name("") is False
        assert kwin.close_window_by_name("   ") is False
        run.assert_not_called()
