"""Targeted tests for MainWindow's TTS coordination — specifically
the MPRIS pause/resume ref-counting that prevents music from
resuming under a still-speaking second utterance.

We use stub objects rather than a real QApplication: the methods
under test are pure Python that touches simple state and the
already-mocked ``_mpris.qdbus`` wire."""
from __future__ import annotations
from unittest.mock import patch

import pytest

from korder.audio import _mpris


class _FakeRecorder:
    """Idle-recorder stub so _snip_tts_bleed_window short-circuits.
    Tests that exercise the recording-active branch swap this out."""
    is_recording = False


class _MainWindowStub:
    """Just enough of MainWindow to exercise the ref-counted pause
    + resume methods. The methods are bound off the real class
    via types.MethodType in each test."""
    def __init__(self):
        self._tts_paused_services: list[str] = []
        self._tts_active_count = 0
        self._recorder = _FakeRecorder()
        # Tests that exercise the duck pause/resume around TTS
        # override this with a real-ish stub. None disables the
        # branch so the MPRIS-only behavior is testable in isolation.
        self._ducker = None


def _bind(stub):
    """Bind _mpris_pause_for_tts, _resume_after_tts,
    _force_resume_after_cancel, _snip_tts_bleed_window from MainWindow
    onto the stub. Works around needing a QApplication for the real
    class."""
    import types
    from korder.ui import main_window as mw
    stub._mpris_pause_for_tts = types.MethodType(mw.MainWindow._mpris_pause_for_tts, stub)
    stub._resume_after_tts = types.MethodType(mw.MainWindow._resume_after_tts, stub)
    stub._force_resume_after_cancel = types.MethodType(mw.MainWindow._force_resume_after_cancel, stub)
    stub._snip_tts_bleed_window = types.MethodType(mw.MainWindow._snip_tts_bleed_window, stub)
    return stub


def test_two_utterances_keep_music_paused_until_both_finish():
    """The bug this guards against: utterance #1 pauses Spotify;
    utterance #2 starts before #1's playback_finished fires; #1's
    playback_finished resumes Spotify even though #2 is still
    playing → music plays underneath voice. With ref counting,
    Spotify only resumes after BOTH have finished."""
    stub = _bind(_MainWindowStub())
    qdbus_calls: list[tuple] = []

    with (
        patch.object(_mpris, "list_players", return_value=["org.mpris.MediaPlayer2.spotify"]),
        patch.object(_mpris, "player_status", return_value="Playing"),
        patch.object(_mpris, "qdbus", side_effect=lambda *a: qdbus_calls.append(a) or ""),
    ):
        # Two TTS calls in rapid succession (both pause-for-tts before
        # either playback_finished fires)
        stub._mpris_pause_for_tts()
        stub._mpris_pause_for_tts()

    # Spotify was paused exactly once (idempotent — no re-pause)
    pause_calls = [c for c in qdbus_calls if c[-1].endswith(".Pause")]
    assert len(pause_calls) == 1, f"expected one Pause; got {pause_calls!r}"

    # First playback_finished arrives. With ref-counting, music
    # should NOT resume yet (count would drop to 1).
    qdbus_calls.clear()
    with patch.object(_mpris, "qdbus", side_effect=lambda *a: qdbus_calls.append(a) or ""):
        stub._resume_after_tts()
    play_calls_after_first = [c for c in qdbus_calls if c[-1].endswith(".Play")]
    assert play_calls_after_first == [], (
        "music resumed after first utterance — should wait for second"
    )
    assert stub._tts_active_count == 1

    # Second playback_finished arrives. NOW music resumes.
    qdbus_calls.clear()
    with patch.object(_mpris, "qdbus", side_effect=lambda *a: qdbus_calls.append(a) or ""):
        stub._resume_after_tts()
    play_calls_after_second = [c for c in qdbus_calls if c[-1].endswith(".Play")]
    assert len(play_calls_after_second) == 1, (
        f"music should resume after second utterance; got {play_calls_after_second!r}"
    )
    assert stub._tts_active_count == 0


def test_resume_below_zero_clamps_safely():
    """Defensive: if playback_finished fires more times than pauses
    happened (shouldn't, but logic must not break), count clamps
    to zero rather than going negative + accumulating debt."""
    stub = _bind(_MainWindowStub())

    with patch.object(_mpris, "qdbus", return_value=""):
        # Resume called without any pause — should be a no-op
        stub._resume_after_tts()

    assert stub._tts_active_count == 0
    assert stub._tts_paused_services == []


def test_force_resume_after_cancel_releases_count_and_services():
    """tts.cancel() drains queued utterances WITHOUT pulling them
    through the worker's playback_finished — so the ref count would
    stay above zero and music would never resume. Force-resume
    zeroes the count and resumes everything."""
    stub = _bind(_MainWindowStub())

    qdbus_calls: list[tuple] = []
    with (
        patch.object(_mpris, "list_players", return_value=["org.mpris.MediaPlayer2.spotify"]),
        patch.object(_mpris, "player_status", return_value="Playing"),
        patch.object(_mpris, "qdbus", side_effect=lambda *a: qdbus_calls.append(a) or ""),
    ):
        # Three "pauses" without any resume — simulates three queued
        # utterances. tts.cancel() drains all three without firing
        # playback_finished for the queued ones.
        stub._mpris_pause_for_tts()
        stub._mpris_pause_for_tts()
        stub._mpris_pause_for_tts()
    assert stub._tts_active_count == 3

    qdbus_calls.clear()
    with patch.object(_mpris, "qdbus", side_effect=lambda *a: qdbus_calls.append(a) or ""):
        stub._force_resume_after_cancel()
    play_calls = [c for c in qdbus_calls if c[-1].endswith(".Play")]
    assert len(play_calls) == 1, (
        f"force-resume should release Spotify exactly once; got {play_calls!r}"
    )
    assert stub._tts_active_count == 0
    assert stub._tts_paused_services == []


def test_cancel_recording_stops_tts_even_when_recorder_idle(monkeypatch):
    """Regression: now_playing triggers auto_stop, recorder closes,
    TTS keeps speaking. User hits Esc — cancel_recording must stop
    TTS even though the recorder is no longer recording.

    Previous bug: cancel_recording early-returned on `not is_recording`
    BEFORE calling tts.cancel(), so the voice kept yapping past
    user's stop signal."""
    import types
    from unittest.mock import MagicMock
    from korder.ui import main_window as mw

    tts = MagicMock()
    tts.is_playing.return_value = True

    recorder = MagicMock()
    recorder.is_recording = False  # auto_stop already closed it

    class _Stub:
        _recorder = recorder
        _tts = tts
        _ducker = None
        _chime_pending_timer = None
        _tts_paused_services = []
        _tts_active_count = 0
        _await_tts_for_answer_reset = False
        _dictation_via_wake = False
        _partial_timer = MagicMock()
        _osd_throttle_timer = MagicMock()
        _wake_idle_timer = MagicMock()
        _osd = MagicMock()
        _status = MagicMock()
        _pending_partial_text = None
        _recent_transcripts = MagicMock()
        def _sync_button(self): pass
        def _emit_tray_state(self): pass

    stub = _Stub()
    stub._force_resume_after_cancel = types.MethodType(
        mw.MainWindow._force_resume_after_cancel, stub,
    )
    stub._snip_tts_bleed_window = types.MethodType(
        mw.MainWindow._snip_tts_bleed_window, stub,
    )
    stub.cancel_recording = types.MethodType(mw.MainWindow.cancel_recording, stub)

    with patch.object(_mpris, "qdbus", return_value=""):
        stub.cancel_recording()

    # TTS.cancel() ran even though recorder wasn't recording
    assert tts.cancel.called, "TTS must be silenced on cancel even when recorder is idle"


def test_resume_after_tts_snips_committed_samples_when_recording():
    """Regression: TTS playback bleeds into the open mic; without
    advancing _committed_samples past the bleed window, the next
    partial-tick transcribes Korder's own voice and re-triggers the
    same TTS — feedback loop. After playback_finished drops the
    count to 0, _committed_samples must jump to the current
    snapshot length so the bleed is skipped."""
    import types
    from unittest.mock import MagicMock
    from korder.ui import main_window as mw

    recorder = MagicMock()
    recorder.is_recording = True
    # Simulate 2 seconds of audio captured during TTS (16 kHz × 2 s).
    fake_buffer = MagicMock()
    fake_buffer.shape = (32000,)
    recorder.snapshot.return_value = fake_buffer

    class _Stub:
        _recorder = recorder
        _tts_paused_services = []
        _tts_active_count = 1  # TTS in flight
        _committed_samples = 0
        _last_partial_norm = "stale"
        _stability_count = 5
        _last_displayed_partial = "stale"
        _pending_partial_text = "stale"
        _last_osd_partial_t = 0.0
        _osd_throttle_timer = MagicMock()
        _ducker = None  # this test isn't exercising the duck path

    stub = _Stub()
    stub._snip_tts_bleed_window = types.MethodType(mw.MainWindow._snip_tts_bleed_window, stub)
    stub._reset_partial_render_state = types.MethodType(
        mw.MainWindow._reset_partial_render_state, stub
    )
    stub._resume_after_tts = types.MethodType(mw.MainWindow._resume_after_tts, stub)

    stub._resume_after_tts()

    assert stub._tts_active_count == 0
    assert stub._committed_samples == 32000, (
        "committed_samples must jump past the TTS bleed window"
    )
    # Stale partial-tracking state cleared so the next partial doesn't
    # try to lock against pre-TTS text.
    assert stub._last_partial_norm == ""
    assert stub._stability_count == 0
    assert stub._last_displayed_partial == ""


def test_resume_after_tts_no_op_when_recorder_idle():
    """TTS played outside a dictation session (e.g. now_playing
    auto-stop tail) has no recorder buffer to snip. Must not raise
    or touch state that doesn't exist."""
    import types
    from unittest.mock import MagicMock
    from korder.ui import main_window as mw

    recorder = MagicMock()
    recorder.is_recording = False

    class _Stub:
        _recorder = recorder
        _tts_paused_services = []
        _tts_active_count = 1
        _ducker = None
        # Deliberately omit _committed_samples / _last_partial_norm /
        # etc. so we'd raise AttributeError if the snip ran.

    stub = _Stub()
    stub._snip_tts_bleed_window = types.MethodType(mw.MainWindow._snip_tts_bleed_window, stub)
    stub._resume_after_tts = types.MethodType(mw.MainWindow._resume_after_tts, stub)

    stub._resume_after_tts()  # must not raise

    assert stub._tts_active_count == 0
    recorder.snapshot.assert_not_called()


def test_late_playback_finished_after_force_resume_is_noop():
    """If a stale playback_finished arrives after force-resume
    (because the worker was mid-play when cancel hit), the count
    has already been zeroed — _resume_after_tts must not flip it
    negative or fire spurious Play commands."""
    stub = _bind(_MainWindowStub())

    qdbus_calls: list[tuple] = []
    with (
        patch.object(_mpris, "list_players", return_value=["org.mpris.MediaPlayer2.spotify"]),
        patch.object(_mpris, "player_status", return_value="Playing"),
        patch.object(_mpris, "qdbus", side_effect=lambda *a: qdbus_calls.append(a) or ""),
    ):
        stub._mpris_pause_for_tts()
        stub._force_resume_after_cancel()
        # State is now: count=0, services=[]. Stale finish arrives:
        qdbus_calls.clear()
        stub._resume_after_tts()
    play_calls_late = [c for c in qdbus_calls if c[-1].endswith(".Play")]
    assert play_calls_late == [], (
        "late playback_finished after force-resume must be a no-op"
    )
    assert stub._tts_active_count == 0
