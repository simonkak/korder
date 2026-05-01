"""Tests for the now_playing action — picker logic + qdbus parsing +
notify-send fan-out. All subprocess calls are mocked so no real D-Bus
or notification daemon is needed."""
from __future__ import annotations
import subprocess
from unittest.mock import patch

import pytest

from korder.actions import now_playing as np_mod
from korder.actions.base import get_action


def _completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


# --- Picker ----------------------------------------------------------------


def test_picker_prefers_playing_over_paused():
    services = [
        "org.mpris.MediaPlayer2.firefox",
        "org.mpris.MediaPlayer2.spotify",
        "org.mpris.MediaPlayer2.mpv",
    ]
    statuses = {
        "org.mpris.MediaPlayer2.firefox": "Paused",
        "org.mpris.MediaPlayer2.spotify": "Playing",
        "org.mpris.MediaPlayer2.mpv": "Stopped",
    }
    with patch.object(np_mod, "_player_status", side_effect=lambda s: statuses[s]):
        assert np_mod._pick_active_player(services) == "org.mpris.MediaPlayer2.spotify"


def test_picker_falls_back_to_paused_when_nothing_playing():
    services = ["org.mpris.MediaPlayer2.firefox", "org.mpris.MediaPlayer2.spotify"]
    statuses = {
        "org.mpris.MediaPlayer2.firefox": "Stopped",
        "org.mpris.MediaPlayer2.spotify": "Paused",
    }
    with patch.object(np_mod, "_player_status", side_effect=lambda s: statuses[s]):
        assert np_mod._pick_active_player(services) == "org.mpris.MediaPlayer2.spotify"


def test_picker_falls_back_to_first_when_no_known_status():
    services = ["org.mpris.MediaPlayer2.foo", "org.mpris.MediaPlayer2.bar"]
    with patch.object(np_mod, "_player_status", return_value=""):
        assert np_mod._pick_active_player(services) == "org.mpris.MediaPlayer2.foo"


def test_picker_returns_none_when_no_players():
    assert np_mod._pick_active_player([]) is None


# --- Metadata parsing ------------------------------------------------------


def test_parse_metadata_title_artist_album():
    out = (
        "mpris:length: 202333000\n"
        "mpris:trackid: /com/spotify/track/abc\n"
        "xesam:album: Blurryface\n"
        "xesam:albumArtist: Twenty One Pilots\n"
        "xesam:artist: Twenty One Pilots\n"
        "xesam:title: Stressed Out\n"
        "xesam:trackNumber: 2\n"
    )
    with patch.object(np_mod, "_qdbus", return_value=out):
        md = np_mod._player_metadata("org.mpris.MediaPlayer2.spotify")
    assert md == {
        "album": "Blurryface",
        "artist": "Twenty One Pilots",
        "title": "Stressed Out",
    }


def test_parse_metadata_handles_missing_fields():
    """If MPRIS reports only a title, we don't fabricate other fields."""
    out = "xesam:title: Just the title\n"
    with patch.object(np_mod, "_qdbus", return_value=out):
        md = np_mod._player_metadata("org.mpris.MediaPlayer2.x")
    assert md == {"title": "Just the title"}


def test_parse_metadata_returns_empty_when_qdbus_fails():
    with patch.object(np_mod, "_qdbus", return_value=None):
        assert np_mod._player_metadata("any") == {}


# --- Service listing -------------------------------------------------------


def test_list_mpris_players_filters_to_mpris_services():
    out = (
        "org.freedesktop.DBus\n"
        " org.mpris.MediaPlayer2.spotify\n"
        " org.mpris.MediaPlayer2.firefox.instance_1_14161\n"
        " org.kde.plasmashell\n"
    )
    with patch.object(np_mod, "_qdbus", return_value=out):
        players = np_mod._list_mpris_players()
    assert players == [
        "org.mpris.MediaPlayer2.spotify",
        "org.mpris.MediaPlayer2.firefox.instance_1_14161",
    ]


def test_list_mpris_players_returns_empty_when_qdbus_missing():
    with patch.object(np_mod, "_qdbus", return_value=None):
        assert np_mod._list_mpris_players() == []


# --- Short player names ----------------------------------------------------


@pytest.mark.parametrize("service, expected", [
    ("org.mpris.MediaPlayer2.spotify", "Spotify"),
    ("org.mpris.MediaPlayer2.firefox.instance_1_14161", "Firefox"),
    ("org.mpris.MediaPlayer2.vlc", "VLC"),
    ("org.mpris.MediaPlayer2.mpv", "mpv"),
    ("org.mpris.MediaPlayer2.plasma-browser-integration", "Browser"),
    ("org.mpris.MediaPlayer2.something-unknown", "Something Unknown"),
])
def test_short_player_name(service, expected):
    assert np_mod._short_player_name(service) == expected


# --- End-to-end _now_playing flow -----------------------------------------


def test_now_playing_fires_notify_with_track_info():
    notif_calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "notify-send":
            notif_calls.append(list(cmd))
            return _completed()
        # qdbus6 is mocked at higher levels
        raise AssertionError(f"unexpected subprocess call: {cmd!r}")

    with (
        patch.object(np_mod, "_list_mpris_players", return_value=["org.mpris.MediaPlayer2.spotify"]),
        patch.object(np_mod, "_player_status", return_value="Playing"),
        patch.object(np_mod, "_player_metadata", return_value={
            "title": "Overcompensate", "artist": "Twenty One Pilots", "album": "Clancy",
        }),
        patch.object(np_mod.subprocess, "run", side_effect=fake_run),
    ):
        np_mod._now_playing()

    assert len(notif_calls) == 1
    # notify-send called with the right title (player + status icon) and body
    args = notif_calls[0]
    title_arg = args[-2]
    body_arg = args[-1]
    assert "Spotify" in title_arg
    assert "▶" in title_arg  # play icon for Playing status
    assert body_arg == "Overcompensate — Twenty One Pilots"


def test_now_playing_says_nothing_playing_when_no_players():
    notif_calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "notify-send":
            notif_calls.append(list(cmd))
            return _completed()
        raise AssertionError(f"unexpected subprocess call: {cmd!r}")

    with (
        patch.object(np_mod, "_list_mpris_players", return_value=[]),
        patch.object(np_mod.subprocess, "run", side_effect=fake_run),
    ):
        np_mod._now_playing()

    assert len(notif_calls) == 1
    assert notif_calls[0][-2] == "Nothing playing"


def test_now_playing_handles_paused_player():
    notif_calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "notify-send":
            notif_calls.append(list(cmd))
            return _completed()
        raise AssertionError(f"unexpected subprocess call: {cmd!r}")

    with (
        patch.object(np_mod, "_list_mpris_players", return_value=["org.mpris.MediaPlayer2.mpv"]),
        patch.object(np_mod, "_player_status", return_value="Paused"),
        patch.object(np_mod, "_player_metadata", return_value={
            "title": "Some Track", "artist": "Some Artist",
        }),
        patch.object(np_mod.subprocess, "run", side_effect=fake_run),
    ):
        np_mod._now_playing()

    assert "⏸" in notif_calls[0][-2]


def test_now_playing_handles_metadata_with_only_title():
    notif_calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "notify-send":
            notif_calls.append(list(cmd))
            return _completed()
        raise AssertionError(f"unexpected subprocess call: {cmd!r}")

    with (
        patch.object(np_mod, "_list_mpris_players", return_value=["org.mpris.MediaPlayer2.x"]),
        patch.object(np_mod, "_player_status", return_value="Playing"),
        patch.object(np_mod, "_player_metadata", return_value={"title": "Solo Track"}),
        patch.object(np_mod.subprocess, "run", side_effect=fake_run),
    ):
        np_mod._now_playing()

    assert notif_calls[0][-1] == "Solo Track"  # no em-dash, no artist


def test_now_playing_survives_missing_notify_send():
    """notify-send not installed → action should not raise."""
    def raise_fnf(cmd, *args, **kwargs):
        raise FileNotFoundError(cmd[0])

    with (
        patch.object(np_mod, "_list_mpris_players", return_value=["org.mpris.MediaPlayer2.spotify"]),
        patch.object(np_mod, "_player_status", return_value="Playing"),
        patch.object(np_mod, "_player_metadata", return_value={"title": "X", "artist": "Y"}),
        patch.object(np_mod.subprocess, "run", side_effect=raise_fnf),
    ):
        np_mod._now_playing()  # must not raise


# --- Action registration ---------------------------------------------------


def test_now_playing_action_is_registered():
    """Action self-registers on import; verify it's reachable from the
    registry with the expected triggers in both languages."""
    action = get_action("now_playing")
    assert action is not None
    en_triggers = action.triggers.get("en", [])
    pl_triggers = action.triggers.get("pl", [])
    assert "what's playing" in en_triggers
    assert "co gra" in pl_triggers


def test_now_playing_op_factory_returns_callable():
    action = get_action("now_playing")
    op = action.op_factory({})
    assert op[0] == "callable"
    assert callable(op[1])
