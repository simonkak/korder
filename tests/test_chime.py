"""Tests for the start-of-listening chime synthesis + playback path.

We don't open a real audio device — sounddevice is mocked so the
tests run anywhere. Verifies: the synthesized buffer has sane
properties (no clicks, normalized peak), playback uses a separate
OutputStream from sd.play's global default (so cancel doesn't
interfere with TTS), and cancel_chime aborts the in-flight stream."""
from __future__ import annotations
import threading
import time
import types
from unittest.mock import MagicMock

import numpy as np
import pytest

from korder.audio import chime as chime_mod


def _reset_chime_state():
    """Tests sometimes need to invalidate the synth cache (the buffer
    is module-scoped) and clear any stream the previous test left."""
    chime_mod._cache.clear()
    with chime_mod._active_stream_lock:
        chime_mod._active_stream = None


def test_build_chime_returns_sane_buffer():
    _reset_chime_state()
    audio, sr, dur_ms = chime_mod.build_start_chime()
    assert sr == 44100
    # Third return is the DEFERRAL window for the caller — the audio
    # itself is longer (bell rings out past the mic-open point at
    # sub-bleed amplitude). dur_ms = 280 ms; audio = ~550 ms.
    assert dur_ms == 280
    assert audio.dtype == np.float32
    assert audio.ndim == 1
    expected_n = int(0.55 * 44100)
    # Allow ±1 sample tolerance on the linspace boundary
    assert abs(audio.shape[0] - expected_n) <= 1
    # Peak amplitude bounded by the documented headroom (~ -13 dBFS)
    peak = float(np.max(np.abs(audio)))
    assert 0.0 < peak <= 0.25, f"peak {peak} outside expected range"
    # Soft attack: first sample near zero (avoids speaker pop)
    assert abs(audio[0]) < peak * 0.5
    # Bell ringdown: the last 50 ms must be substantially quieter
    # than the loud body (~first 100 ms). Sanity check that the
    # exponential envelope is wired up — not a precise acoustic
    # claim. Real speaker-bleed margin is provided by the ducker
    # taking system volume to 30 % at mic-open.
    body_peak = float(np.max(np.abs(audio[: int(0.10 * 44100)])))
    tail_peak = float(np.max(np.abs(audio[-int(0.05 * 44100):])))
    assert tail_peak < body_peak * 0.5, (
        f"tail {tail_peak} should be much quieter than body {body_peak}"
    )


def test_build_chime_caches_buffer():
    """Repeated calls return the same buffer object — ~35 KB, no
    point re-synthesizing on every chime."""
    _reset_chime_state()
    a, _, _ = chime_mod.build_start_chime()
    b, _, _ = chime_mod.build_start_chime()
    assert a is b


def test_play_chime_uses_dedicated_output_stream(monkeypatch):
    """Crucial: chime must NOT use sd.play (the global default
    stream) — TTS uses sd.OutputStream too, and cancel must be
    able to stop the chime without killing TTS."""
    _reset_chime_state()
    constructed: list[MagicMock] = []

    def fake_outputstream(**kwargs):
        m = MagicMock()
        constructed.append(m)
        return m

    monkeypatch.setattr(chime_mod.sd, "OutputStream", fake_outputstream)
    # Ensure sd.play is NOT called
    monkeypatch.setattr(chime_mod.sd, "play", MagicMock(side_effect=AssertionError("chime must not use sd.play")))

    duration_ms = chime_mod.play_start_chime()
    assert duration_ms == 280
    # OutputStream constructed exactly once with the right format
    assert len(constructed) == 1
    # start() called on it
    assert constructed[0].start.called

    # Wait for the writer thread to land (it calls write + cleanup)
    deadline = time.time() + 1.0
    while time.time() < deadline:
        if constructed[0].close.called:
            break
        time.sleep(0.02)
    assert constructed[0].write.called
    assert constructed[0].close.called


def test_play_chime_returns_zero_on_outputstream_failure(monkeypatch):
    """When the audio device is busy / unavailable, the chime path
    must not crash — return 0 so the caller proceeds without it."""
    _reset_chime_state()

    def fake_outputstream(**kwargs):
        raise RuntimeError("PortAudioError: Stream busy")

    monkeypatch.setattr(chime_mod.sd, "OutputStream", fake_outputstream)
    duration_ms = chime_mod.play_start_chime()
    assert duration_ms == 0


def test_cancel_chime_aborts_in_flight_stream(monkeypatch):
    """Cancel during playback should call stream.abort() so the
    deferred recorder.start doesn't fire on a stranded chime."""
    _reset_chime_state()

    # Use an Event to keep write() blocking until we trigger cancel.
    write_started = threading.Event()
    write_unblock = threading.Event()

    fake_stream = MagicMock()

    def slow_write(_):
        write_started.set()
        write_unblock.wait(timeout=2.0)

    fake_stream.write.side_effect = slow_write

    def fake_outputstream(**kwargs):
        return fake_stream

    monkeypatch.setattr(chime_mod.sd, "OutputStream", fake_outputstream)
    chime_mod.play_start_chime()
    assert write_started.wait(0.5), "writer thread should have started"

    # Cancel mid-playback
    chime_mod.cancel_chime()

    # abort + close were called on our specific stream
    assert fake_stream.abort.called
    # Let the writer thread exit so the test runner cleans up.
    write_unblock.set()


def test_cancel_chime_no_op_when_nothing_playing():
    """Calling cancel_chime when the chime isn't playing must be
    a no-op — used defensively from cancel paths that don't know
    the chime state."""
    _reset_chime_state()
    # Nothing playing — should not raise.
    chime_mod.cancel_chime()


# --- Integration with MainWindow capture sequencing ----------------------


def test_main_window_defers_recorder_start_until_chime_finishes(monkeypatch):
    """Verifies the central contract: when start_chime is enabled,
    recorder.start() must NOT be called until after the chime's
    duration window. This is the speaker-bleed-prevention property —
    if we open the mic during the chime, Whisper transcribes its
    tones."""
    from korder.ui import main_window as mw

    # Patch QTimer so we don't need a real Qt event loop. The fake
    # timer captures (interval, callback) so we can fire it manually.
    fired_timers: list[tuple[int, object]] = []

    class FakeTimer:
        def __init__(self, parent=None):
            self._cb = None
            self._interval = 0
            self._single_shot = False
        def setSingleShot(self, v):
            self._single_shot = v
        def timeout(self):
            return self
        @property
        def timeout_signal(self):
            return self
        def connect(self, cb):
            self._cb = cb
        def start(self, interval):
            self._interval = interval
            fired_timers.append((interval, self._cb))
        def stop(self):
            pass

    # Two attribute paths: .timeout.connect() — requires a property
    # that returns something with .connect. Adjust:
    class FakeSignal:
        def __init__(self, owner):
            self._owner = owner
        def connect(self, cb):
            self._owner._cb = cb

    class FakeTimer2:
        def __init__(self, parent=None):
            self._cb = None
            self._single_shot = False
            self.timeout = FakeSignal(self)
        def setSingleShot(self, v):
            self._single_shot = v
        def start(self, interval):
            fired_timers.append((interval, self._cb))
        def stop(self):
            pass

    monkeypatch.setattr(mw, "QTimer", FakeTimer2)

    recorder = MagicMock()
    recorder.is_recording = False
    start_calls: list[float] = []

    def fake_start():
        start_calls.append(time.monotonic())
        recorder.is_recording = True
    recorder.start.side_effect = fake_start

    chime_calls: list[None] = []
    def fake_play_chime():
        chime_calls.append(None)
        return 100  # 100ms
    monkeypatch.setattr(
        "korder.audio.chime.play_start_chime", fake_play_chime,
    )

    # Stub for everything the real method touches
    class _Stub:
        _recorder = recorder
        _tts = None
        _start_chime_enabled = True
        _chime_pending_timer = None
        _injector = None
        _dictation_via_wake = False
        _wake_idle_timeout_s = 0
        _partial_in_flight = False
        _committed_samples = 0
        _last_partial_norm = ""
        _stability_count = 0
        _write_mode = False
        _osd = MagicMock()
        _partial_timer = MagicMock()
        _wake_idle_timer = MagicMock()
        _status = MagicMock()
        def _reset_partial_render_state(self): pass
        def _begin_dictation_lifecycle(self): pass
        def _sync_button(self): pass
        def _emit_tray_state(self): pass
        def statusBar(self): return MagicMock()

    stub = _Stub()
    # Bind the real _begin_capture as a method on our stub so
    # _start_recording's `self._begin_capture()` call resolves.
    stub._begin_capture = types.MethodType(mw.MainWindow._begin_capture, stub)
    mw.MainWindow._start_recording(stub)

    assert chime_calls == [None], "chime should have been triggered"
    assert start_calls == [], "recorder.start ran before chime finished — speaker bleed risk"
    assert stub._chime_pending_timer is not None
    assert fired_timers, "a deferred timer was scheduled"
    interval_ms, cb = fired_timers[0]
    assert interval_ms == 150  # 100ms chime + 50ms slack

    # Now simulate the timer firing — recorder.start should run
    cb()
    assert len(start_calls) == 1, "recorder.start should fire when chime timer expires"
    assert stub._chime_pending_timer is None  # cleared after _begin_capture


def test_main_window_skips_chime_when_disabled(monkeypatch):
    """When [audio] start_chime = false, _start_recording should
    bypass the chime entirely and start the recorder immediately."""
    from korder.ui import main_window as mw

    recorder = MagicMock()
    recorder.is_recording = False
    start_calls: list[None] = []
    def fake_start():
        start_calls.append(None)
        recorder.is_recording = True
    recorder.start.side_effect = fake_start

    chime_calls: list[None] = []
    monkeypatch.setattr(
        "korder.audio.chime.play_start_chime",
        lambda: chime_calls.append(None) or 100,
    )

    class _Stub:
        _recorder = recorder
        _tts = None
        _start_chime_enabled = False  # ← disabled
        _chime_pending_timer = None
        _injector = None
        _dictation_via_wake = False
        _wake_idle_timeout_s = 0
        _partial_in_flight = False
        _committed_samples = 0
        _last_partial_norm = ""
        _stability_count = 0
        _write_mode = False
        _osd = MagicMock()
        _partial_timer = MagicMock()
        _wake_idle_timer = MagicMock()
        _status = MagicMock()
        def _reset_partial_render_state(self): pass
        def _begin_dictation_lifecycle(self): pass
        def _sync_button(self): pass
        def _emit_tray_state(self): pass
        def statusBar(self): return MagicMock()

    stub = _Stub()
    stub._begin_capture = types.MethodType(mw.MainWindow._begin_capture, stub)
    mw.MainWindow._start_recording(stub)

    assert chime_calls == [], "chime should not have fired when disabled"
    assert len(start_calls) == 1, "recorder.start should fire immediately"