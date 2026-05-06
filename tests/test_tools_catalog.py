"""Tests for the v1 tool catalog: list_audio_sinks,
list_paired_bluetooth_devices, list_active_mpris_players. Each tool's
executor is exercised against mocked underlying helpers so the test
is independent of the host's PipeWire / BlueZ / MPRIS state."""
from __future__ import annotations
from unittest.mock import patch

import korder.tools  # noqa: F401  (force registration)
from korder.tools.base import get_tool


# ---- list_audio_sinks ----------------------------------------------------


def test_list_audio_sinks_returns_canonical_shape():
    """Wraps _list_sinks and re-emits as [{name, is_default}, …]."""
    with patch(
        "korder.actions.audio_output._list_sinks",
        return_value=([(59, "Głośniki monitora"), (95, "Denon DHT-S517")], 95),
    ):
        result = get_tool("list_audio_sinks").executor()
    assert result == [
        {"name": "Głośniki monitora", "is_default": False},
        {"name": "Denon DHT-S517", "is_default": True},
    ]


def test_list_audio_sinks_empty_on_failure():
    """A failing helper must not propagate — the tool returns an
    empty list and the loop continues."""
    with patch(
        "korder.actions.audio_output._list_sinks",
        side_effect=RuntimeError("wpctl missing"),
    ):
        assert get_tool("list_audio_sinks").executor() == []


# ---- list_paired_bluetooth_devices ---------------------------------------


def test_list_paired_bluetooth_devices_marks_connected():
    """Connected list intersected against Paired produces the
    `connected` flag per device."""
    def fake_list(filter_arg):
        if filter_arg == "Paired":
            return [
                ("AA:00:00:00:00:01", "Denon DHT-S517"),
                ("BB:00:00:00:00:02", "PXC 550-II"),
            ]
        if filter_arg == "Connected":
            return [("AA:00:00:00:00:01", "Denon DHT-S517")]
        return []

    with patch(
        "korder.actions.bluetooth._list_devices",
        side_effect=fake_list,
    ):
        result = get_tool("list_paired_bluetooth_devices").executor()
    assert result == [
        {"name": "Denon DHT-S517", "mac": "AA:00:00:00:00:01", "connected": True},
        {"name": "PXC 550-II", "mac": "BB:00:00:00:00:02", "connected": False},
    ]


def test_list_paired_bluetooth_devices_empty_on_failure():
    with patch(
        "korder.actions.bluetooth._list_devices",
        side_effect=RuntimeError("bluetoothctl missing"),
    ):
        assert get_tool("list_paired_bluetooth_devices").executor() == []


# ---- list_active_mpris_players -------------------------------------------


def test_list_active_mpris_players_includes_metadata():
    """Each MPRIS service contributes a {short_name, status, title,
    artist} dict. Empty title/artist when player has no metadata."""
    with (
        patch(
            "korder.audio._mpris.list_players",
            return_value=["org.mpris.MediaPlayer2.spotify", "org.mpris.MediaPlayer2.firefox"],
        ),
        patch(
            "korder.audio._mpris.short_player_name",
            side_effect=lambda s: "Spotify" if "spotify" in s else "Firefox",
        ),
        patch(
            "korder.audio._mpris.player_status",
            side_effect=lambda s: "Playing" if "spotify" in s else "Paused",
        ),
        patch(
            "korder.audio._mpris.player_metadata",
            side_effect=lambda s: (
                {"title": "Numb", "artist": "Linkin Park"} if "spotify" in s
                else {"title": "How I Chose a Distro", "artist": ""}
            ),
        ),
    ):
        result = get_tool("list_active_mpris_players").executor()
    assert result == [
        {"short_name": "Spotify", "status": "Playing", "title": "Numb", "artist": "Linkin Park"},
        {"short_name": "Firefox", "status": "Paused", "title": "How I Chose a Distro", "artist": ""},
    ]


def test_list_active_mpris_players_empty_when_no_services():
    with patch("korder.audio._mpris.list_players", return_value=[]):
        assert get_tool("list_active_mpris_players").executor() == []


# ---- search_spotify ------------------------------------------------------


def test_search_spotify_proxies_to_client_with_kwargs():
    """The tool wraps SpotifyClient.search_top_n. Args from the LLM
    arrive as kwargs; query is required, kind is optional."""
    from korder.actions import spotify as spotify_action
    fake_client = type("F", (), {})()
    captured: list[tuple] = []
    fake_client.search_top_n = lambda q, kind, n: (
        captured.append((q, kind, n)) or [
            {"uri": "spotify:track:abc", "name": "Numb", "kind": "track", "artist": "Linkin Park"},
        ]
    )
    with patch.object(spotify_action, "_get_client", return_value=fake_client):
        result = get_tool("search_spotify").executor(query="Numb", kind="track")
    assert captured == [("Numb", "track", 5)]
    assert result == [
        {"uri": "spotify:track:abc", "name": "Numb", "kind": "track", "artist": "Linkin Park"},
    ]


def test_search_spotify_strips_whitespace_and_normalizes_kind():
    """Defensive: the LLM occasionally pads strings or sends the kind
    in the wrong case. The tool sanitizes both before hitting the
    client."""
    from korder.actions import spotify as spotify_action
    fake_client = type("F", (), {})()
    captured: list[tuple] = []
    fake_client.search_top_n = lambda q, kind, n: (
        captured.append((q, kind, n)) or []
    )
    with patch.object(spotify_action, "_get_client", return_value=fake_client):
        get_tool("search_spotify").executor(query="  Workout  ", kind="PLAYLIST")
    assert captured == [("Workout", "playlist", 5)]


def test_search_spotify_empty_query_short_circuits():
    """No query → return [] without hitting the client at all. Saves
    a network round-trip when the LLM accidentally calls with empty
    args."""
    from korder.actions import spotify as spotify_action
    with patch.object(spotify_action, "_get_client") as get_client:
        result = get_tool("search_spotify").executor(query="")
    assert result == []
    get_client.assert_not_called()


def test_search_spotify_empty_when_no_credentials():
    """If the user hasn't configured Spotify API creds, _get_client
    returns None. The tool returns [] cleanly so the loop just sees
    no candidates and the action's fallback (open Spotify search UI)
    handles dispatch."""
    from korder.actions import spotify as spotify_action
    with patch.object(spotify_action, "_get_client", return_value=None):
        result = get_tool("search_spotify").executor(query="anything")
    assert result == []


def test_search_spotify_drops_invalid_kind():
    """Kind values outside the enum (case-insensitive) get dropped to
    None — the client then searches across all four types."""
    from korder.actions import spotify as spotify_action
    fake_client = type("F", (), {})()
    captured: list[tuple] = []
    fake_client.search_top_n = lambda q, kind, n: (
        captured.append((q, kind, n)) or []
    )
    with patch.object(spotify_action, "_get_client", return_value=fake_client):
        get_tool("search_spotify").executor(query="X", kind="vinyl")
    # kind="vinyl" isn't valid — passed through as None.
    assert captured == [("X", None, 5)]


# ---- list_open_windows ---------------------------------------------------


def test_list_open_windows_passes_through_kwin_bridge_shape():
    """The tool wraps kwin_bridge.list_windows and re-emits the same
    {resourceClass, caption, active, minimized} fields the always-on
    renderer used to consume."""
    fake_windows = [
        {"id": "u1", "caption": "Firefox PR", "resourceClass": "firefox", "active": True, "minimized": False},
        {"id": "u2", "caption": "Konsole", "resourceClass": "konsole", "active": False, "minimized": False},
        {"id": "u3", "caption": "Spotify", "resourceClass": "spotify", "active": False, "minimized": True},
    ]
    with patch("korder.kwin_bridge.list_windows", return_value=fake_windows):
        result = get_tool("list_open_windows").executor()
    assert result == [
        {"resourceClass": "firefox", "caption": "Firefox PR", "active": True, "minimized": False},
        {"resourceClass": "konsole", "caption": "Konsole", "active": False, "minimized": False},
        {"resourceClass": "spotify", "caption": "Spotify", "active": False, "minimized": True},
    ]


def test_list_open_windows_filters_unusable_entries():
    """Windows with neither caption nor resourceClass have nothing for
    the LLM to match against — drop them. Same defensive filter the
    old _render_window_list applied."""
    fake_windows = [
        {"id": "u1", "caption": "", "resourceClass": "", "active": False, "minimized": False},
        {"id": "u2", "caption": "Real", "resourceClass": "app", "active": False, "minimized": False},
    ]
    with patch("korder.kwin_bridge.list_windows", return_value=fake_windows):
        result = get_tool("list_open_windows").executor()
    assert len(result) == 1
    assert result[0]["caption"] == "Real"


def test_list_open_windows_empty_on_failure():
    with patch(
        "korder.kwin_bridge.list_windows",
        side_effect=RuntimeError("dbus down"),
    ):
        assert get_tool("list_open_windows").executor() == []


def test_list_active_mpris_players_skips_failing_service():
    """A per-service exception shouldn't kill the whole tool — log
    and skip that service, return the others."""
    with (
        patch(
            "korder.audio._mpris.list_players",
            return_value=["good", "broken"],
        ),
        patch(
            "korder.audio._mpris.short_player_name",
            side_effect=lambda s: "Good" if s == "good" else (_ for _ in ()).throw(RuntimeError("dbus")),
        ),
        patch("korder.audio._mpris.player_status", return_value="Playing"),
        patch("korder.audio._mpris.player_metadata", return_value={"title": "T", "artist": "A"}),
    ):
        result = get_tool("list_active_mpris_players").executor()
    # Only the working service shows up. The broken one is dropped silently.
    assert len(result) == 1
    assert result[0]["short_name"] == "Good"
