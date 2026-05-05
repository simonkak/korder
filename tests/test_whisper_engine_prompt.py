"""WhisperEngine.transcribe per-call initial_prompt plumbing.

The actual whisper.cpp model is mocked — these tests verify the
prompt is forwarded to pywhispercpp's Model.transcribe call (and the
right fallback fires when nothing is supplied)."""
from __future__ import annotations
from unittest.mock import MagicMock

import numpy as np
import pytest

from korder.transcribe.whisper_engine import WhisperEngine


@pytest.fixture
def engine_with_mock_model():
    eng = WhisperEngine(model="tiny", language="en", initial_prompt="static-context")
    fake = MagicMock()
    fake.transcribe.return_value = []
    eng._model = fake
    return eng, fake


def _audio_with_speech_frames(n: int = 16000) -> np.ndarray:
    """Wide-band noise loud enough that webrtcvad classifies most
    frames as speech — satisfies the engine's frame-ratio gate so
    the model actually gets called."""
    rng = np.random.default_rng(0)
    return (0.3 * rng.standard_normal(n)).astype(np.float32)


def test_per_call_prompt_overrides_static(engine_with_mock_model):
    eng, fake = engine_with_mock_model
    eng.transcribe(_audio_with_speech_frames(), initial_prompt="rolling-context")
    assert fake.transcribe.called
    _, kwargs = fake.transcribe.call_args
    assert kwargs["initial_prompt"] == "rolling-context"


def test_no_per_call_prompt_falls_back_to_static(engine_with_mock_model):
    eng, fake = engine_with_mock_model
    eng.transcribe(_audio_with_speech_frames())
    _, kwargs = fake.transcribe.call_args
    assert kwargs["initial_prompt"] == "static-context"


def test_empty_per_call_prompt_replaces_static_with_empty(engine_with_mock_model):
    """An explicit empty string is treated as 'no context' for THIS
    call — needed so the rolling-context deque can clear the prompt
    without falling back to the static one."""
    eng, fake = engine_with_mock_model
    eng.transcribe(_audio_with_speech_frames(), initial_prompt="")
    _, kwargs = fake.transcribe.call_args
    assert kwargs["initial_prompt"] == ""


def test_silent_audio_short_circuits_before_model_call(engine_with_mock_model):
    """The energy gate runs before the model call, so a quiet buffer
    never invokes whisper — and no prompt mutation happens."""
    eng, fake = engine_with_mock_model
    silence = np.zeros(16000, dtype=np.float32)
    out = eng.transcribe(silence, initial_prompt="anything")
    assert out == ""
    assert not fake.transcribe.called
