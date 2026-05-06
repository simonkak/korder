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
