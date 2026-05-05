"""Silence-trim tests for SpeechDetector."""
from __future__ import annotations
import numpy as np
import pytest

from korder.audio.vad import SpeechDetector


SR = 16000


def _silence(duration_s: float) -> np.ndarray:
    return np.zeros(int(duration_s * SR), dtype=np.float32)


def _speechy_noise(duration_s: float, seed: int = 42) -> np.ndarray:
    """Wide-band noise loud enough that webrtcvad classifies most
    frames as speech. Real speech has a richer spectrum but the
    detector's threshold is on energy, so this exercises the same
    code path."""
    rng = np.random.default_rng(seed)
    return (0.3 * rng.standard_normal(int(duration_s * SR))).astype(np.float32)


@pytest.fixture
def det():
    return SpeechDetector(sample_rate=SR, aggressiveness=2)


def test_find_leading_silence_locates_speech_start(det):
    buf = np.concatenate([_silence(0.5), _speechy_noise(0.5)])
    idx = det.find_leading_silence(buf)
    # Speech starts at 0.5 s; allow up to one frame of slack.
    assert 0.45 * SR <= idx <= 0.55 * SR


def test_find_leading_silence_returns_size_for_pure_silence(det):
    buf = _silence(0.5)
    assert det.find_leading_silence(buf) == buf.size


def test_trim_silence_removes_leading_and_trailing_with_guard(det):
    buf = np.concatenate([_silence(0.5), _speechy_noise(0.5), _silence(0.5)])
    trimmed = det.trim_silence(buf, guard_ms=150)
    # 500 ms speech + 2×150 ms guard = ~800 ms; webrtcvad can flag a
    # frame or two past the abrupt noise/silence boundary as speech,
    # so allow up to ~1 s on the upper bound. The point of the test
    # is that trimming happened at all (size < buf.size = 1.5 s).
    assert 0.7 * SR <= trimmed.size <= 1.0 * SR
    assert trimmed.size < buf.size


def test_trim_silence_no_speech_returns_input(det):
    buf = _silence(1.0)
    trimmed = det.trim_silence(buf)
    np.testing.assert_array_equal(trimmed, buf)


def test_trim_silence_empty_returns_empty(det):
    empty = np.zeros(0, dtype=np.float32)
    trimmed = det.trim_silence(empty)
    assert trimmed.size == 0


def test_trim_silence_guard_does_not_underflow_at_start(det):
    """Speech that starts in the very first frame: guard_ms can't pull
    the start index below zero."""
    buf = np.concatenate([_speechy_noise(0.3), _silence(0.3)])
    trimmed = det.trim_silence(buf, guard_ms=150)
    # No leading silence means the trim begins at sample 0.
    assert trimmed.size <= buf.size
    assert trimmed.size > 0
