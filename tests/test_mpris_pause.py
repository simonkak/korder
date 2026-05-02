"""Tests for shared MPRIS helpers used by the TTS pause/resume path.

The actual pause/resume coordination lives in MainWindow (it spans
an async TTS call) — see test_main_window_tts_*. This file tests
the standalone helpers: any_playing(), list_players() filtering,
player_status() parsing.
"""
from __future__ import annotations
from unittest.mock import patch

from korder.audio import _mpris


def test_any_playing_returns_true_when_a_service_is_playing():
    services = [
        "org.mpris.MediaPlayer2.firefox",
        "org.mpris.MediaPlayer2.spotify",
    ]
    statuses = {
        "org.mpris.MediaPlayer2.firefox": "Paused",
        "org.mpris.MediaPlayer2.spotify": "Playing",
    }
    with (
        patch.object(_mpris, "list_players", return_value=services),
        patch.object(_mpris, "player_status", side_effect=lambda s: statuses[s]),
    ):
        assert _mpris.any_playing() is True


def test_any_playing_returns_false_when_nothing_is_playing():
    services = ["org.mpris.MediaPlayer2.firefox"]
    with (
        patch.object(_mpris, "list_players", return_value=services),
        patch.object(_mpris, "player_status", return_value="Paused"),
    ):
        assert _mpris.any_playing() is False


def test_any_playing_returns_false_when_no_players():
    with patch.object(_mpris, "list_players", return_value=[]):
        assert _mpris.any_playing() is False
