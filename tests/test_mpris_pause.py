"""Tests for the MPRIS pause/resume context manager used by the TTS
path. We don't shell out to qdbus6; we patch korder.audio._mpris.qdbus
and assert on the calls."""
from __future__ import annotations
from unittest.mock import patch, call

from korder.audio import _mpris


def test_paused_for_tts_pauses_only_playing_services():
    """Three players: one Playing, one Paused, one Stopped. Only the
    Playing one should be paused on entry, and resumed on exit."""
    services = [
        "org.mpris.MediaPlayer2.firefox",   # Paused
        "org.mpris.MediaPlayer2.spotify",   # Playing
        "org.mpris.MediaPlayer2.mpv",       # Stopped
    ]
    statuses = {
        "org.mpris.MediaPlayer2.firefox": "Paused",
        "org.mpris.MediaPlayer2.spotify": "Playing",
        "org.mpris.MediaPlayer2.mpv": "Stopped",
    }
    qdbus_calls: list[tuple] = []

    def fake_qdbus(*args):
        qdbus_calls.append(args)
        # The Pause / Play calls return empty stdout; the status
        # query is mocked separately.
        return ""

    with (
        patch.object(_mpris, "list_players", return_value=services),
        patch.object(_mpris, "player_status", side_effect=lambda s: statuses[s]),
        patch.object(_mpris, "qdbus", side_effect=fake_qdbus),
    ):
        with _mpris.paused_for_tts() as paused:
            assert paused == ["org.mpris.MediaPlayer2.spotify"]
            # Pause was called for spotify only
            assert call(
                "org.mpris.MediaPlayer2.spotify",
                _mpris.MPRIS_OBJECT,
                f"{_mpris.PLAYER_IFACE}.Pause",
            ) in [call(*a) for a in qdbus_calls]

    # On exit, Play is called for the same service
    play_calls = [a for a in qdbus_calls if a[-1].endswith(".Play")]
    assert play_calls == [(
        "org.mpris.MediaPlayer2.spotify",
        _mpris.MPRIS_OBJECT,
        f"{_mpris.PLAYER_IFACE}.Play",
    )]


def test_paused_for_tts_no_op_when_nothing_playing():
    """All players Paused or Stopped → no Pause / Play calls."""
    services = [
        "org.mpris.MediaPlayer2.firefox",
        "org.mpris.MediaPlayer2.mpv",
    ]
    statuses = {
        "org.mpris.MediaPlayer2.firefox": "Paused",
        "org.mpris.MediaPlayer2.mpv": "Stopped",
    }
    qdbus_calls: list[tuple] = []

    with (
        patch.object(_mpris, "list_players", return_value=services),
        patch.object(_mpris, "player_status", side_effect=lambda s: statuses[s]),
        patch.object(_mpris, "qdbus", side_effect=lambda *a: qdbus_calls.append(a) or ""),
    ):
        with _mpris.paused_for_tts() as paused:
            assert paused == []

    # No state-changing qdbus calls were made
    assert all(not a[-1].endswith(".Pause") for a in qdbus_calls)
    assert all(not a[-1].endswith(".Play") for a in qdbus_calls)


def test_paused_for_tts_resumes_only_paused_services_on_exception():
    """If the with-block raises, we should still resume what we paused.
    Tests cleanup symmetry."""
    services = ["org.mpris.MediaPlayer2.spotify"]
    play_calls: list[tuple] = []

    def fake_qdbus(*args):
        if args[-1].endswith(".Play"):
            play_calls.append(args)
        return ""

    with (
        patch.object(_mpris, "list_players", return_value=services),
        patch.object(_mpris, "player_status", return_value="Playing"),
        patch.object(_mpris, "qdbus", side_effect=fake_qdbus),
    ):
        try:
            with _mpris.paused_for_tts():
                raise RuntimeError("boom")
        except RuntimeError:
            pass

    assert play_calls == [(
        "org.mpris.MediaPlayer2.spotify",
        _mpris.MPRIS_OBJECT,
        f"{_mpris.PLAYER_IFACE}.Play",
    )]


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
