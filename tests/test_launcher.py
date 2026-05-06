"""Tests for the app_launcher action — mocks the .desktop scan and
the launch subprocess so the tests are filesystem-independent."""
from __future__ import annotations
from unittest.mock import patch

import korder.actions  # noqa: F401  (default registrations)
from korder.actions.base import get_action
from korder.actions import launcher


def _set_index(entries: list[tuple[str, str, set[str]]]) -> None:
    """Inject a synthetic .desktop index for resolver tests."""
    launcher._DESKTOP_CACHE = entries


def _reset_index() -> None:
    launcher._invalidate_desktop_cache()


# ---- _resolve_app --------------------------------------------------------


def test_resolve_app_picks_stem_match_for_common_name():
    """The user says 'firefox' → match stems 'firefox' (token-equal
    to a meaningful >=4 char stem token)."""
    _set_index([
        ("firefox", "Firefox", {"firefox", "browser", "web"}),
        ("org.kde.konsole", "Konsole", {"org", "kde", "konsole", "terminal"}),
    ])
    try:
        assert launcher._resolve_app("firefox") == ("firefox", "Firefox")
        assert launcher._resolve_app("konsole") == ("org.kde.konsole", "Konsole")
    finally:
        _reset_index()


def test_resolve_app_handles_dotted_stem():
    """'spotify' should match 'com.spotify.Client' via the stem
    fragment 'spotify' (the meaningful token in the dotted name)."""
    _set_index([
        ("com.spotify.Client", "Spotify", {"com", "spotify", "client", "music"}),
    ])
    try:
        assert launcher._resolve_app("spotify") == ("com.spotify.Client", "Spotify")
    finally:
        _reset_index()


def test_resolve_app_multi_word_query_uses_field_overlap():
    """'visual studio code' should match a Code .desktop via
    multi-token overlap on Name/GenericName even when the stem is
    just 'code'."""
    _set_index([
        ("code", "Visual Studio Code", {"code", "visual", "studio", "editor"}),
        ("firefox", "Firefox", {"firefox", "browser"}),
    ])
    try:
        assert launcher._resolve_app("visual studio code") == ("code", "Visual Studio Code")
    finally:
        _reset_index()


def test_resolve_app_rejects_weak_short_token_overlap():
    """Field log: 'krunner-not-an-app' shouldn't resolve to
    org.kde.kdeconnect.app via the 'app' overlap. The 4-char
    minimum on stem-match tokens kills this class of false
    positive."""
    _set_index([
        ("org.kde.kdeconnect.app", "KDE Connect", {"org", "kde", "kdeconnect", "app", "connect"}),
        ("firefox", "Firefox", {"firefox", "browser"}),
    ])
    try:
        assert launcher._resolve_app("krunner-not-an-app") is None
    finally:
        _reset_index()


def test_resolve_app_returns_none_on_no_overlap():
    _set_index([
        ("firefox", "Firefox", {"firefox", "browser"}),
    ])
    try:
        assert launcher._resolve_app("totally fake app name") is None
        assert launcher._resolve_app("") is None
    finally:
        _reset_index()


def test_resolve_app_prefers_stem_match_over_field_overlap():
    """Two candidates: one with stem='firefox' and one with stem
    something else but multiple field-token overlaps. Stem match
    wins because it gets 2x weight."""
    _set_index([
        ("firefox", "Firefox", {"firefox"}),
        ("other-app", "Other Browser", {"other", "browser", "firefox"}),
    ])
    try:
        # 'firefox' stem-match scores 2 (1 stem * 2x = 2, +1 all = 3).
        # other-app: stem_overlap_meaningful=0, all_overlap=1 → score 1.
        assert launcher._resolve_app("firefox") == ("firefox", "Firefox")
    finally:
        _reset_index()


# ---- action registration + dispatch -------------------------------------


def test_app_launcher_action_registered():
    action = get_action("app_launcher")
    assert action is not None
    assert "app_name" in action.parameters
    assert action.parameters["app_name"].get("required") is True


def test_app_launcher_op_with_empty_name_returns_pending():
    """No app named → factory returns None, MainWindow flips to
    pending-action mode and waits for the next utterance to fill
    the param."""
    action = get_action("app_launcher")
    assert action.op_factory({}) is None
    assert action.op_factory({"app_name": ""}) is None
    assert action.op_factory({"app_name": "  "}) is None


def test_app_launcher_op_dispatches_resolved_app():
    """Resolved query → gtk-launch <stem>. Confirms the (stem, display)
    pair from _resolve_app reaches the launch path."""
    _set_index([
        ("firefox", "Firefox", {"firefox", "browser"}),
    ])
    captured: list[list[str]] = []
    try:
        with (
            patch("korder.actions.launcher.shutil.which", side_effect=lambda c: c if c == "gtk-launch" else None),
            patch("korder.actions.launcher.subprocess.Popen", side_effect=lambda c, **kw: captured.append(c) or None),
        ):
            action = get_action("app_launcher")
            kind, fn = action.op_factory({"app_name": "firefox"})
            assert kind == "callable"
            fn()
        assert captured == [["gtk-launch", "firefox"]]
    finally:
        _reset_index()


def test_app_launcher_falls_back_to_krunner_when_no_match():
    """Unresolved query → KRunner D-Bus query, user disambiguates
    in the launcher UI."""
    _set_index([
        ("firefox", "Firefox", {"firefox"}),  # only firefox installed
    ])
    captured: list[list[str]] = []
    try:
        # gtk-launch path doesn't fire because resolver returns None.
        # Only krunner subprocess should run.
        with (
            patch("korder.actions.launcher.shutil.which", side_effect=lambda c: c if c == "qdbus6" else None),
            patch(
                "korder.actions.launcher.subprocess.run",
                side_effect=lambda c, **kw: captured.append(c) or _OkRun(),
            ),
        ):
            action = get_action("app_launcher")
            _, fn = action.op_factory({"app_name": "totally not installed"})
            fn()
        assert any("org.kde.krunner" in arg for cmd in captured for arg in cmd), (
            f"expected krunner D-Bus call; got {captured!r}"
        )
    finally:
        _reset_index()


class _OkRun:
    """Minimal subprocess.CompletedProcess stand-in for tests."""
    returncode = 0
    stdout = ""
    stderr = ""
