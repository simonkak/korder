"""Tests for the dual mic-suppression gate (signal-driven count +
authoritative is_playing query) and the fallback timer's
re-arm-when-still-speaking behavior. Defends against the bug where
'Dziękuję' was transcribed during TTS playback because the
playback_finished signal missed and the ref count got stuck."""
from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import numpy as np

from korder.ui import main_window as mw


# ---- Mic gating during TTS (signal-independent) --------------------------


def test_partial_tick_gated_when_tts_engine_actually_playing():
    """Belt-and-suspenders gate: even with _tts_active_count=0
    (signal lost or never fired), if the TTS engine reports it's
    currently playing audio, the partial tick must abort. Stops the
    open mic from transcribing the bleed when the ref count is
    stranded due to a missed playback_finished signal."""
    fake_tts = MagicMock()
    fake_tts.is_playing.return_value = True

    recorder = MagicMock()
    recorder.is_recording = True

    osd = MagicMock()
    osd._state.stateKind = "listening"  # mic gate would otherwise admit

    class _Stub:
        _recorder = recorder
        _osd = osd
        _tts = fake_tts
        _WHISPER_ACTIVE_STATES = mw.MainWindow._WHISPER_ACTIVE_STATES
        # signal-driven count is 0 (the "missed signal" failure)
        _tts_active_count = 0
        _committed_samples = 0
        # Anything below would only be touched if the gate let us through —
        # leave undefined so a false negative raises AttributeError loudly.

    stub = _Stub()
    stub._on_partial_tick = types.MethodType(mw.MainWindow._on_partial_tick, stub)
    stub._on_partial_tick()

    # Snapshot is taken after both gates pass — confirm we returned before that.
    recorder.snapshot.assert_not_called()


def test_partial_tick_admits_when_tts_idle_and_count_zero():
    """The gate must NOT short-circuit when neither signal says TTS
    is active. Otherwise the user can never speak again."""
    fake_tts = MagicMock()
    fake_tts.is_playing.return_value = False

    recorder = MagicMock()
    recorder.is_recording = True
    recorder.sample_rate = 16000
    recorder.snapshot.return_value = np.zeros(16000, dtype=np.float32)

    osd = MagicMock()
    osd._state.stateKind = "listening"

    detector = MagicMock()
    detector.find_trailing_silence.return_value = (0, 0)
    detector.has_speech.return_value = False

    class _Stub:
        _recorder = recorder
        _osd = osd
        _tts = fake_tts
        _detector = detector
        _WHISPER_ACTIVE_STATES = mw.MainWindow._WHISPER_ACTIVE_STATES
        MIN_SPEECH_FOR_PARTIAL_MS = mw.MainWindow.MIN_SPEECH_FOR_PARTIAL_MS
        MIN_COMMIT_MS = mw.MainWindow.MIN_COMMIT_MS
        PAUSE_MS = mw.MainWindow.PAUSE_MS
        MAX_SEGMENT_MS = mw.MainWindow.MAX_SEGMENT_MS
        _tts_active_count = 0
        _committed_samples = 0
        _wake_idle_timer = MagicMock()
        _wake_idle_timer.isActive.return_value = False
        _partial_in_flight = False

    stub = _Stub()
    stub._on_partial_tick = types.MethodType(mw.MainWindow._on_partial_tick, stub)
    stub._on_partial_tick()

    # Snapshot was called → we got past both TTS gates.
    recorder.snapshot.assert_called_once()


# ---- Fallback timer re-arming when TTS still playing ---------------------


def test_fallback_answer_reset_rearms_when_tts_still_playing():
    """Originally the fallback timer hard-released the listening
    state after read_ms+8s, even if TTS was genuinely still
    talking (long synth, slow device). That created a window where
    the OSD said 'listening' while audio was still coming out of
    the speaker. With this fix, the fallback re-arms instead."""
    fake_tts = MagicMock()
    fake_tts.is_playing.return_value = True  # still talking

    class _Stub:
        _await_tts_for_answer_reset = True
        _tts = fake_tts
        # If reset ran, _reset_to_listening_after_answer would touch
        # recorder etc. — leave undefined so an accidental call raises.

    stub = _Stub()
    stub._fallback_answer_reset = types.MethodType(
        mw.MainWindow._fallback_answer_reset, stub
    )

    with patch.object(mw.QTimer, "singleShot") as single_shot:
        stub._fallback_answer_reset()

    # Re-armed, not flipped.
    assert stub._await_tts_for_answer_reset is True
    single_shot.assert_called_once()
    # Argument 0 is the delay; we just want it positive (re-armed).
    assert single_shot.call_args.args[0] > 0


def test_fallback_answer_reset_releases_when_tts_idle():
    """When TTS truly is idle (signal genuinely lost, engine done),
    the fallback must do its job — flip the flag and reset."""
    fake_tts = MagicMock()
    fake_tts.is_playing.return_value = False

    reset_called: list[bool] = []

    class _Stub:
        _await_tts_for_answer_reset = True
        _tts = fake_tts
        def _reset_to_listening_after_answer(self):
            reset_called.append(True)

    stub = _Stub()
    stub._fallback_answer_reset = types.MethodType(
        mw.MainWindow._fallback_answer_reset, stub
    )

    stub._fallback_answer_reset()

    assert stub._await_tts_for_answer_reset is False
    assert reset_called == [True]


# ---- Per-TTS MPRIS pause is no longer needed -----------------------------


def test_mpris_pause_for_tts_does_not_pause_ducker_or_mpris():
    """Music is paused for the whole dictation lifecycle by the
    ducker (MPRIS-pause-based since the simplification). Per-TTS
    pause is therefore redundant — and would interfere with the
    ducker's resume on dictation end if it tried to track its own
    paused-services list. The TTS pause path is now purely
    bookkeeping for the in-flight ref count."""
    ducker = MagicMock()

    class _Stub:
        _tts_active_count = 0
        _ducker = ducker

    stub = _Stub()
    stub._mpris_pause_for_tts = types.MethodType(
        mw.MainWindow._mpris_pause_for_tts, stub
    )

    with patch.object(mw._mpris, "qdbus") as qdbus:
        stub._mpris_pause_for_tts()

    assert stub._tts_active_count == 1
    ducker.pause.assert_not_called()
    ducker.duck.assert_not_called()
    ducker.restore.assert_not_called()
    qdbus.assert_not_called()
