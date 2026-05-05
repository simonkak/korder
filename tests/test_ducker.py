"""VolumeDucker tests with mocked MPRIS calls. Verifies the
duck/restore lifecycle pauses Playing media services and resumes
them on dictation end.

The earlier wpctl-based ducker lowered the default sink's volume
during recording. The current ducker switches to MPRIS pause/resume
so external music is fully silent during listening (better for
Whisper) and Korder's own TTS plays at the user's normal volume
(no lift/re-engage dance per TTS event)."""
from __future__ import annotations
from unittest.mock import patch

from korder.audio import _mpris
from korder.audio.ducker import VolumeDucker


def test_disabled_ducker_does_nothing():
    with patch.object(_mpris, "list_players") as list_players:
        d = VolumeDucker(enabled=False)
        d.duck()
        d.restore()
    list_players.assert_not_called()


def test_duck_pauses_only_playing_services():
    """Stopped or already-paused services are left alone — we don't
    want to surprise-pause something the user explicitly chose to
    keep stopped, and resume() would later un-stop it incorrectly."""
    qdbus_calls: list[tuple] = []

    def fake_qdbus(*args):
        qdbus_calls.append(args)
        return ""

    statuses = {
        "org.mpris.MediaPlayer2.spotify": "Playing",
        "org.mpris.MediaPlayer2.mpv": "Paused",
        "org.mpris.MediaPlayer2.firefox": "Stopped",
    }

    with (
        patch.object(_mpris, "list_players", return_value=list(statuses.keys())),
        patch.object(_mpris, "player_status", side_effect=lambda s: statuses[s]),
        patch.object(_mpris, "qdbus", side_effect=fake_qdbus),
    ):
        d = VolumeDucker(enabled=True)
        d.duck()

    # Only the Playing service got paused
    pause_calls = [c for c in qdbus_calls if c[-1].endswith(".Pause")]
    assert len(pause_calls) == 1
    assert "spotify" in pause_calls[0][0]


def test_restore_resumes_paused_services():
    qdbus_calls: list[tuple] = []

    def fake_qdbus(*args):
        qdbus_calls.append(args)
        return ""

    with (
        patch.object(_mpris, "list_players", return_value=["org.mpris.MediaPlayer2.spotify"]),
        patch.object(_mpris, "player_status", return_value="Playing"),
        patch.object(_mpris, "qdbus", side_effect=fake_qdbus),
    ):
        d = VolumeDucker(enabled=True)
        d.duck()
        qdbus_calls.clear()
        d.restore()

    play_calls = [c for c in qdbus_calls if c[-1].endswith(".Play")]
    assert len(play_calls) == 1


def test_duck_is_idempotent():
    """A second duck() while already paused must NOT re-pause —
    that'd risk re-listing already-paused services as 'we paused
    them' and resuming them spuriously."""
    qdbus_calls: list[tuple] = []

    def fake_qdbus(*args):
        qdbus_calls.append(args)
        return ""

    with (
        patch.object(_mpris, "list_players", return_value=["org.mpris.MediaPlayer2.spotify"]),
        patch.object(_mpris, "player_status", return_value="Playing"),
        patch.object(_mpris, "qdbus", side_effect=fake_qdbus),
    ):
        d = VolumeDucker(enabled=True)
        d.duck()
        first = list(qdbus_calls)
        d.duck()
        assert qdbus_calls == first, "second duck() must be a no-op"


def test_restore_without_duck_is_noop():
    qdbus_calls: list[tuple] = []
    with (
        patch.object(_mpris, "list_players", return_value=[]),
        patch.object(_mpris, "qdbus", side_effect=lambda *a: qdbus_calls.append(a) or ""),
    ):
        d = VolumeDucker(enabled=True)
        d.restore()
    assert qdbus_calls == []


def test_duck_handles_player_pause_failure_continues_with_others():
    """One stuck player shouldn't block the rest from being paused —
    the user's music still gets paused even if a misbehaving widget
    refuses to cooperate."""
    qdbus_calls: list[tuple] = []

    def fake_qdbus(svc, *_rest):
        qdbus_calls.append(svc)
        if "stuck" in svc:
            return None  # _mpris convention: None = call failed
        return ""

    with (
        patch.object(_mpris, "list_players", return_value=[
            "org.mpris.MediaPlayer2.stuck",
            "org.mpris.MediaPlayer2.spotify",
        ]),
        patch.object(_mpris, "player_status", return_value="Playing"),
        patch.object(_mpris, "qdbus", side_effect=fake_qdbus),
    ):
        d = VolumeDucker(enabled=True)
        d.duck()
        # Both attempted, only spotify recorded as paused (stuck returned None)
        d.restore()
        # Only spotify gets a Play call — we never claimed to pause stuck
        play_calls = [c for c in qdbus_calls if "spotify" in c]
        assert len(play_calls) == 2  # one Pause + one Play
        stuck_calls = [c for c in qdbus_calls if "stuck" in c]
        assert len(stuck_calls) == 1  # only the failed Pause


def test_target_pct_constructor_arg_is_accepted_for_compat():
    """Old call sites pass target_pct=N; the constructor must accept
    and ignore it without raising. Removable after one release."""
    d = VolumeDucker(enabled=False, target_pct=30)
    assert d._enabled is False


# ---- release() — user takes ownership of a service mid-session -----------


def test_release_drops_service_from_resume_list():
    """The bug this guards against: ducker pauses Firefox at
    dictation start; user says 'pause Firefox' (a no-op since it's
    already paused); session ends; ducker.restore() auto-resumes
    Firefox — undoing the user's intent. release() lets the action
    claim ownership so restore skips that service."""
    qdbus_calls: list[tuple] = []

    def fake_qdbus(*args):
        qdbus_calls.append(args)
        return ""

    with (
        patch.object(_mpris, "list_players", return_value=[
            "org.mpris.MediaPlayer2.spotify",
            "org.mpris.MediaPlayer2.firefox",
        ]),
        patch.object(_mpris, "player_status", return_value="Playing"),
        patch.object(_mpris, "qdbus", side_effect=fake_qdbus),
    ):
        d = VolumeDucker(enabled=True)
        d.duck()
        # User explicitly paused Firefox via pause_player; that action
        # claims ownership.
        d.release("org.mpris.MediaPlayer2.firefox")
        qdbus_calls.clear()
        d.restore()

    play_calls = [c for c in qdbus_calls if c[-1].endswith(".Play")]
    play_targets = [c[0] for c in play_calls]
    assert "org.mpris.MediaPlayer2.spotify" in play_targets
    assert "org.mpris.MediaPlayer2.firefox" not in play_targets


def test_release_unknown_service_is_noop():
    """release() on a service we never paused must not raise — the
    user might pause something the ducker didn't touch (already-Paused
    when dictation started)."""
    d = VolumeDucker(enabled=True)
    d.release("org.mpris.MediaPlayer2.never-saw-this")  # must not raise


def test_release_from_session_pause_helper_uses_active_ducker():
    """Module-level helper plumbed for actions that don't hold a
    direct ducker reference. Registers the ducker once at app
    startup; the helper consults that registration."""
    from korder.audio.ducker import (
        register_active_ducker,
        release_from_session_pause,
    )
    qdbus_calls: list[tuple] = []
    with (
        patch.object(_mpris, "list_players", return_value=["org.mpris.MediaPlayer2.firefox"]),
        patch.object(_mpris, "player_status", return_value="Playing"),
        patch.object(_mpris, "qdbus", side_effect=lambda *a: qdbus_calls.append(a) or ""),
    ):
        d = VolumeDucker(enabled=True)
        d.duck()
        register_active_ducker(d)
        try:
            release_from_session_pause("org.mpris.MediaPlayer2.firefox")
            qdbus_calls.clear()
            d.restore()
        finally:
            register_active_ducker(None)

    play_calls = [c for c in qdbus_calls if c[-1].endswith(".Play")]
    assert play_calls == []


def test_release_from_session_pause_with_no_registered_ducker_is_noop():
    """Headless / test contexts may not register an active ducker.
    The helper must silently do nothing."""
    from korder.audio.ducker import release_from_session_pause, register_active_ducker

    register_active_ducker(None)
    release_from_session_pause("anything")  # must not raise
