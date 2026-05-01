"""WakeWordDetector tests. The openwakeword Model is mocked so the
tests exercise the buffering, threshold, cooldown, and signal-firing
logic without needing the real ONNX model or audio thread."""
from __future__ import annotations
import sys
import types
from unittest.mock import MagicMock

import numpy as np
import pytest


def _install_fake_openwakeword(monkeypatch, predict_returns):
    """Inject a fake openwakeword.model.Model into sys.modules so the
    deferred import inside WakeWordDetector.start() picks it up.
    `predict_returns` is a callable invoked with each predict() chunk
    and must return a dict {phrase: score}."""
    fake_model = MagicMock()
    fake_model.predict.side_effect = predict_returns

    def fake_factory(wakeword_models):
        # Round-trip the phrase so callers can assert the model was
        # asked for the right one.
        fake_model.requested = list(wakeword_models)
        return fake_model

    fake_module = types.ModuleType("openwakeword.model")
    fake_module.Model = fake_factory

    fake_top = types.ModuleType("openwakeword")
    fake_top.model = fake_module

    monkeypatch.setitem(sys.modules, "openwakeword", fake_top)
    monkeypatch.setitem(sys.modules, "openwakeword.model", fake_module)
    return fake_model


def _frame(samples: int, fill: float = 0.1) -> np.ndarray:
    return np.full(samples, fill, dtype=np.float32)


@pytest.fixture
def fake_recorder():
    """A MicRecorder stand-in that just records subscribe/unsubscribe
    calls so the test can later push frames to the detector by calling
    its registered callback directly."""
    rec = MagicMock()
    rec.sample_rate = 16000
    return rec


def test_start_loads_model_and_subscribes(monkeypatch, fake_recorder):
    from korder.audio.wake import WakeWordDetector

    fake_model = _install_fake_openwakeword(monkeypatch, predict_returns=lambda _c: {"hey_jarvis": 0.0})
    det = WakeWordDetector(fake_recorder, phrase="hey_jarvis", sensitivity=0.5)
    det.start()

    assert det.is_running
    assert fake_model.requested == ["hey_jarvis"]
    fake_recorder.subscribe.assert_called_once_with(det._on_frame)


def test_stop_unsubscribes_and_keeps_model_loaded(monkeypatch, fake_recorder):
    from korder.audio.wake import WakeWordDetector

    _install_fake_openwakeword(monkeypatch, predict_returns=lambda _c: {"hey_jarvis": 0.0})
    det = WakeWordDetector(fake_recorder, phrase="hey_jarvis", sensitivity=0.5)
    det.start()
    det.stop()

    assert not det.is_running
    fake_recorder.unsubscribe.assert_called_once_with(det._on_frame)
    # Model stays around so a subsequent start() doesn't reload.
    assert det._model is not None


def test_below_threshold_does_not_fire(monkeypatch, fake_recorder):
    from korder.audio.wake import WakeWordDetector

    _install_fake_openwakeword(monkeypatch, predict_returns=lambda _c: {"hey_jarvis": 0.2})
    det = WakeWordDetector(fake_recorder, phrase="hey_jarvis", sensitivity=0.5)
    det.start()

    fired: list[bool] = []
    det.detected.connect(lambda: fired.append(True))

    # Push exactly one full chunk (CHUNK_SAMPLES = 1280) — should run
    # predict once and not fire.
    det._on_frame(_frame(WakeWordDetector.CHUNK_SAMPLES))

    assert fired == []


def test_above_threshold_fires_once(monkeypatch, fake_recorder):
    from korder.audio.wake import WakeWordDetector

    _install_fake_openwakeword(monkeypatch, predict_returns=lambda _c: {"hey_jarvis": 0.9})
    det = WakeWordDetector(fake_recorder, phrase="hey_jarvis", sensitivity=0.5)
    det.start()

    fired: list[bool] = []
    det.detected.connect(lambda: fired.append(True))

    det._on_frame(_frame(WakeWordDetector.CHUNK_SAMPLES))

    assert fired == [True]


def test_cooldown_suppresses_repeat_fires(monkeypatch, fake_recorder):
    """A single phrase that scores high across consecutive chunks
    should fire exactly once until the cooldown window elapses."""
    from korder.audio.wake import WakeWordDetector

    _install_fake_openwakeword(monkeypatch, predict_returns=lambda _c: {"hey_jarvis": 0.9})
    det = WakeWordDetector(fake_recorder, phrase="hey_jarvis", sensitivity=0.5)
    det.start()

    fired: list[bool] = []
    det.detected.connect(lambda: fired.append(True))

    # Push 5 consecutive chunks, all scoring high. Only the first
    # should fire (cooldown is ~12 chunks at 80 ms each).
    for _ in range(5):
        det._on_frame(_frame(WakeWordDetector.CHUNK_SAMPLES))

    assert fired == [True]


def test_partial_frames_buffer_until_full(monkeypatch, fake_recorder):
    """Frames smaller than CHUNK_SAMPLES are accumulated; predict only
    fires once a full chunk is available."""
    from korder.audio.wake import WakeWordDetector

    predict_calls: list[int] = []
    def predict(chunk):
        predict_calls.append(len(chunk))
        return {"hey_jarvis": 0.0}

    _install_fake_openwakeword(monkeypatch, predict_returns=predict)
    det = WakeWordDetector(fake_recorder, phrase="hey_jarvis", sensitivity=0.5)
    det.start()

    # Half a chunk — no predict yet.
    det._on_frame(_frame(WakeWordDetector.CHUNK_SAMPLES // 2))
    assert predict_calls == []

    # Other half — now predict fires once.
    det._on_frame(_frame(WakeWordDetector.CHUNK_SAMPLES // 2))
    assert predict_calls == [WakeWordDetector.CHUNK_SAMPLES]


def test_inference_error_emits_error_signal(monkeypatch, fake_recorder):
    from korder.audio.wake import WakeWordDetector

    def boom(_chunk):
        raise RuntimeError("ONNX is sad")

    _install_fake_openwakeword(monkeypatch, predict_returns=boom)
    det = WakeWordDetector(fake_recorder, phrase="hey_jarvis", sensitivity=0.5)
    det.start()

    errors: list[str] = []
    det.error.connect(errors.append)
    det._on_frame(_frame(WakeWordDetector.CHUNK_SAMPLES))

    assert errors and "ONNX is sad" in errors[0]


def test_missing_extra_raises_clear_import_error(monkeypatch, fake_recorder):
    """If openwakeword isn't installed, start() should fail with a
    message that points the user at the install command."""
    from korder.audio.wake import WakeWordDetector

    monkeypatch.delitem(sys.modules, "openwakeword", raising=False)
    monkeypatch.delitem(sys.modules, "openwakeword.model", raising=False)
    # Force the deferred import to fail.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("openwakeword"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    det = WakeWordDetector(fake_recorder, phrase="hey_jarvis", sensitivity=0.5)
    with pytest.raises(ImportError, match="uv sync --extra wake"):
        det.start()
