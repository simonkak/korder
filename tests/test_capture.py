"""MicRecorder fan-out tests. The audio stream itself is mocked — the
tests verify the subscriber-list and dictation-buffer semantics, which
are the parts wake-word activation will rely on."""
from __future__ import annotations
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from korder.audio.capture import MicRecorder


def _frame(n: int = 160, fill: float = 0.1) -> np.ndarray:
    """Mock a 10 ms frame at 16 kHz mono float32, shaped (n, 1)."""
    return np.full((n, 1), fill, dtype=np.float32)


@pytest.fixture
def recorder(monkeypatch):
    """MicRecorder with sd.InputStream mocked. The fake stream tracks
    start/stop/close so tests can assert the underlying audio engine
    runs only while subscribers are attached."""
    from korder.audio import capture

    fake_stream = MagicMock()
    factory_calls: list[dict] = []

    def fake_factory(**kwargs):
        factory_calls.append(kwargs)
        return fake_stream

    monkeypatch.setattr(capture.sd, "InputStream", fake_factory)
    rec = MicRecorder(sample_rate=16000, gain=1.0)
    return rec, fake_stream, factory_calls


def test_start_stop_round_trip(recorder):
    """Existing dictation API: start, simulate audio, stop returns
    concatenated frames; is_recording reflects the active session."""
    rec, stream, calls = recorder
    assert not rec.is_recording
    rec.start()
    assert rec.is_recording
    assert len(calls) == 1, "stream should open exactly once on first subscriber"
    stream.start.assert_called_once()

    rec._callback(_frame(fill=0.1), 160, None, None)
    rec._callback(_frame(fill=0.2), 160, None, None)

    snap = rec.snapshot()
    assert snap.shape == (320,)
    assert snap[0] == pytest.approx(0.1)
    assert snap[200] == pytest.approx(0.2)

    full = rec.stop()
    assert full.shape == (320,)
    assert not rec.is_recording
    stream.stop.assert_called_once()
    stream.close.assert_called_once()


def test_subscriber_runs_alongside_dictation(recorder):
    """The dictation buffer and an external subscriber both receive each
    frame — this is the fan-out wake-word activation depends on."""
    rec, _stream, _calls = recorder
    seen: list[np.ndarray] = []
    rec.subscribe(seen.append)
    rec.start()

    rec._callback(_frame(fill=0.5), 160, None, None)

    assert len(seen) == 1
    assert seen[0][0] == pytest.approx(0.5)
    full = rec.stop()
    assert full.shape == (160,)
    assert full[0] == pytest.approx(0.5)
    # Subscriber stayed attached after stop() — mic is still open.
    assert rec._stream is not None
    rec.unsubscribe(seen.append)
    assert rec._stream is None


def test_stream_stays_open_until_last_subscriber_leaves(recorder):
    """The audio stream tracks subscriber count, not dictation state.
    stop() during a session shouldn't close the mic if a wake-word
    listener is still attached."""
    rec, stream, calls = recorder
    wake_seen: list[np.ndarray] = []

    rec.subscribe(wake_seen.append)
    assert rec._stream is not None
    assert len(calls) == 1

    rec.start()
    rec._callback(_frame(), 160, None, None)
    rec.stop()
    # Stream must still be open — wake_seen.append is still subscribed.
    assert rec._stream is not None
    stream.stop.assert_not_called()

    rec.unsubscribe(wake_seen.append)
    stream.stop.assert_called_once()
    assert rec._stream is None


def test_subscriber_error_does_not_kill_others(recorder, capsys):
    """One bad subscriber shouldn't deny the others their frames."""
    rec, _stream, _calls = recorder
    seen: list[float] = []

    def bad(_chunk):
        raise RuntimeError("boom")

    rec.subscribe(bad)
    rec.subscribe(lambda c: seen.append(float(c[0])))
    rec._callback(_frame(fill=0.7), 160, None, None)

    assert seen == [pytest.approx(0.7)]
    err = capsys.readouterr().err
    assert "mic subscriber error" in err and "boom" in err


def test_unsubscribe_unknown_callable_is_noop(recorder):
    rec, _stream, _calls = recorder
    rec.unsubscribe(lambda c: None)  # never subscribed; must not raise
    rec.subscribe(lambda c: None)
    rec.unsubscribe(lambda c: None)  # different lambda; must not raise


def test_double_start_is_idempotent(recorder):
    """start() while already recording should not re-open the stream
    or re-install the dictation collector."""
    rec, _stream, calls = recorder
    rec.start()
    rec.start()
    assert len(calls) == 1
    rec._callback(_frame(fill=0.3), 160, None, None)
    full = rec.stop()
    # Single 160-sample frame, not duplicated.
    assert full.shape == (160,)


def test_stop_without_start_returns_empty(recorder):
    rec, _stream, _calls = recorder
    out = rec.stop()
    assert out.shape == (0,)
    assert out.dtype == np.float32


def test_snapshot_without_recording_returns_empty(recorder):
    rec, _stream, _calls = recorder
    out = rec.snapshot()
    assert out.shape == (0,)


def test_unsubscribe_does_not_hold_lock_during_stream_stop(monkeypatch):
    """Regression: PortAudio's stream.stop()/.close() block until the
    current audio callback finishes; the callback wants self._lock to
    snapshot subscribers. If unsubscribe() holds self._lock across
    stream.stop(), the main thread deadlocks against the audio thread.

    Simulate by giving the fake stream a stop() that tries to acquire
    self._lock non-blockingly — if the lock is still held when stop()
    runs, the assertion fires."""
    from korder.audio import capture

    rec = MicRecorder(sample_rate=16000, gain=1.0)
    lock_was_free_during_stop = []

    def fake_stop():
        lock_was_free_during_stop.append(rec._lock.acquire(blocking=False))
        if lock_was_free_during_stop[-1]:
            rec._lock.release()

    fake_stream = MagicMock()
    fake_stream.stop.side_effect = fake_stop
    monkeypatch.setattr(capture.sd, "InputStream", lambda **kw: fake_stream)

    rec.start()
    rec.stop()

    assert lock_was_free_during_stop == [True], (
        "unsubscribe held self._lock across stream.stop() — would deadlock "
        "against the audio callback"
    )


def test_gain_applied_to_frames(monkeypatch):
    """When gain != 1.0, frames are scaled before they reach
    subscribers (and the dictation buffer)."""
    from korder.audio import capture
    monkeypatch.setattr(capture.sd, "InputStream", lambda **kw: MagicMock())
    rec = MicRecorder(sample_rate=16000, gain=2.0)
    seen: list[float] = []
    rec.subscribe(lambda c: seen.append(float(c[0])))
    rec._callback(_frame(fill=0.25), 160, None, None)
    assert seen == [pytest.approx(0.5)]
