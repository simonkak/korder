"""Tests for the engine-level speech-frame gate and annotation
strip — both fixes for Whisper hallucinations on silence/noise
('Dziękuje', '*dźwięk*', etc.)."""
from __future__ import annotations
from unittest.mock import MagicMock

import numpy as np
import pytest

from korder.transcribe.whisper_engine import (
    WhisperEngine,
    _has_speech_frames,
    _strip_annotations,
)


SR = 16000


@pytest.fixture
def engine_with_mock_model():
    eng = WhisperEngine(model="tiny", language="pl")
    fake = MagicMock()
    fake.transcribe.return_value = []
    eng._model = fake
    return eng, fake


def _silence(duration_s: float) -> np.ndarray:
    return np.zeros(int(duration_s * SR), dtype=np.float32)


def _quiet_hum(duration_s: float, rms: float = 0.003) -> np.ndarray:
    """Low-amplitude noise — passes the OLD RMS gate at 0.005 only
    if rms is above; here we set it to 0.003 which previously slipped
    through into Whisper's hallucination zone (too quiet for real
    speech, loud enough to confuse the energy threshold)."""
    rng = np.random.default_rng(0)
    n = int(duration_s * SR)
    sig = (rms * rng.standard_normal(n)).astype(np.float32)
    return sig


def _speechy(duration_s: float) -> np.ndarray:
    """Loud wide-band noise — webrtcvad classifies most frames as
    speech (real speech has a richer spectrum but webrtcvad's
    threshold is on energy, so this exercises the same code path)."""
    rng = np.random.default_rng(0)
    n = int(duration_s * SR)
    return (0.3 * rng.standard_normal(n)).astype(np.float32)


# ---- _has_speech_frames -------------------------------------------------

def test_speech_frames_rejects_silence():
    assert _has_speech_frames(_silence(2.0)) is False


def test_speech_frames_rejects_short_burst_in_long_silence():
    """1.5 s silence + 50 ms noise burst + 1.5 s silence — the burst
    is too short to satisfy min_speech_ms=150 AND its frame-ratio
    contribution is well under 10%. This is the canonical 'cough in
    a quiet room' case that previously made Whisper say 'Dziękuję'."""
    sig = np.concatenate([
        _silence(1.5),
        _speechy(0.05),
        _silence(1.5),
    ])
    assert _has_speech_frames(sig) is False


def test_speech_frames_accepts_real_speech_chunk():
    """A solid 0.5 s burst of speech-classified noise easily clears
    both thresholds."""
    assert _has_speech_frames(_speechy(0.5)) is True


def test_speech_frames_handles_empty():
    assert _has_speech_frames(np.zeros(0, dtype=np.float32)) is False


def test_speech_frames_handles_subframe_buffer():
    """Buffer shorter than one 30 ms frame can't possibly contain
    a speech streak — must reject without dividing by zero."""
    tiny = np.zeros(100, dtype=np.float32)
    assert _has_speech_frames(tiny) is False


# ---- _strip_annotations -------------------------------------------------

def test_strip_bracketed_annotation():
    assert _strip_annotations("hello [music] world") == "hello world"


def test_strip_asterisk_annotation():
    """Regression: '*dźwięk*' previously survived into the LLM
    parser as text content."""
    assert _strip_annotations("*dźwięk*") == ""
    assert _strip_annotations("hello *applause* world") == "hello world"


def test_strip_handles_polish_diacritics_inside_asterisks():
    assert _strip_annotations("*śmiech*") == ""
    assert _strip_annotations("test *muzyka w tle* end") == "test end"


def test_strip_leaves_normal_text_alone():
    assert _strip_annotations("Cześć, jak się masz?") == "Cześć, jak się masz?"


def test_strip_collapses_whitespace_after_strip():
    assert _strip_annotations("a [tag1] [tag2] b") == "a b"


def test_strip_handles_only_annotations():
    assert _strip_annotations("[music] *applause*") == ""


# ---- end-to-end: silence buffer never reaches whisper ------------------

def test_engine_short_circuits_quiet_hum(engine_with_mock_model):
    """The bug we're fixing: a long quiet buffer with hum used to
    pass the 0.005 RMS gate and trigger Whisper hallucinations.
    The new frame-ratio gate rejects it before the model runs."""
    eng, fake = engine_with_mock_model
    out = eng.transcribe(_quiet_hum(3.0))
    assert out == ""
    assert not fake.transcribe.called


def test_engine_passes_real_speech_to_model(engine_with_mock_model):
    eng, fake = engine_with_mock_model
    eng.transcribe(_speechy(1.0))
    assert fake.transcribe.called
