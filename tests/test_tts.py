"""Tests for SpeechEngine — language routing, queue ordering, cancel.

We never actually load Piper or play audio. The PiperVoice import is
patched at module level so tests don't require piper-tts to be
installed (mirrors how tests/test_wake.py handles openwakeword)."""
from __future__ import annotations
import threading
import time
from unittest.mock import MagicMock, patch

from korder.audio import tts as tts_mod


def test_disabled_engine_say_is_no_op():
    """enabled=False → say() doesn't queue anything; the worker
    thread isn't even started."""
    engine = tts_mod.SpeechEngine(
        enabled=False,
        voice_en="en_US-amy-medium",
        voice_pl="pl_PL-darkman-medium",
    )
    engine.say("hello")
    assert not engine.is_playing()
    assert not engine.is_available()  # disabled is the dominant signal


def test_detect_lang_picks_polish_on_diacritics():
    assert tts_mod._detect_lang("Małomiasteczkowy") == "pl"
    assert tts_mod._detect_lang("Stressed Out") == "en"
    assert tts_mod._detect_lang("Ile to siedem razy osiem? Pięćdziesiąt sześć.") == "pl"


def test_voices_available_returns_empty_when_no_data_dir(monkeypatch, tmp_path):
    """No ~/.local/share/piper directory → empty list, no crash."""
    monkeypatch.setattr(tts_mod, "_PIPER_DATA_DIR", tmp_path / "nonexistent")
    engine = tts_mod.SpeechEngine(
        enabled=False,
        voice_en="en_US-amy-medium",
        voice_pl="pl_PL-darkman-medium",
    )
    assert engine.voices_available("en") == []
    assert engine.voices_available("pl") == []


def test_voices_available_lists_downloaded_voices(monkeypatch, tmp_path):
    """Both flat and per-voice-subdir layouts get picked up."""
    # Flat layout: voice.onnx + voice.onnx.json sit at the top level
    (tmp_path / "en_US-amy-medium.onnx").touch()
    (tmp_path / "en_US-amy-medium.onnx.json").touch()
    # Per-voice-subdir layout
    sub = tmp_path / "pl_PL-darkman-medium"
    sub.mkdir()
    (sub / "pl_PL-darkman-medium.onnx").touch()
    (sub / "pl_PL-darkman-medium.onnx.json").touch()
    # Different language — must NOT show up in en results
    (tmp_path / "de_DE-foo.onnx").touch()
    (tmp_path / "de_DE-foo.onnx.json").touch()

    monkeypatch.setattr(tts_mod, "_PIPER_DATA_DIR", tmp_path)
    engine = tts_mod.SpeechEngine(
        enabled=False,
        voice_en="en_US-amy-medium",
        voice_pl="pl_PL-darkman-medium",
    )
    assert engine.voices_available("en") == ["en_US-amy-medium"]
    assert engine.voices_available("pl") == ["pl_PL-darkman-medium"]


def test_voice_files_detects_both_layouts(monkeypatch, tmp_path):
    monkeypatch.setattr(tts_mod, "_PIPER_DATA_DIR", tmp_path)
    # Flat
    flat = tmp_path / "en_US-amy-medium.onnx"
    flat_cfg = tmp_path / "en_US-amy-medium.onnx.json"
    flat.touch()
    flat_cfg.touch()
    found = tts_mod._voice_files("en_US-amy-medium")
    assert found == (flat, flat_cfg)


def test_voice_files_returns_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(tts_mod, "_PIPER_DATA_DIR", tmp_path)
    assert tts_mod._voice_files("missing-voice") is None
    assert tts_mod._voice_files("") is None


class _FakeConfig:
    """Stand-in for piper.config.PiperConfig — only the bits we read."""
    sample_rate = 22050


def _install_fake_piper(monkeypatch, voice_factory):
    """Install a fake `piper` module that exposes PiperVoice via the
    factory and a SynthesisConfig dataclass-shaped helper. Keeps the
    fixture aligned with Piper 1.2's actual import surface."""
    import sys
    fake_piper = MagicMock()
    fake_piper.PiperVoice = voice_factory

    fake_config_module = MagicMock()
    class FakeSynthConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
    fake_config_module.SynthesisConfig = FakeSynthConfig

    monkeypatch.setitem(sys.modules, "piper", fake_piper)
    monkeypatch.setitem(sys.modules, "piper.config", fake_config_module)


def test_say_routes_to_voice_for_language(monkeypatch, tmp_path):
    """When enabled with a mocked PiperVoice + disabled playback,
    say('text', 'pl') should load the PL voice and synthesize."""
    monkeypatch.setattr(tts_mod, "_PIPER_DATA_DIR", tmp_path)
    # Stage both voices on disk so _voice_files succeeds
    for vid in ("en_US-amy-medium", "pl_PL-darkman-medium"):
        (tmp_path / f"{vid}.onnx").touch()
        (tmp_path / f"{vid}.onnx.json").touch()

    fake_voice_load_calls: list[str] = []
    fake_synth_calls: list[tuple[str, object]] = []

    class FakePiperVoice:
        config = _FakeConfig()

        @classmethod
        def load(cls, model_path, config_path=None):
            fake_voice_load_calls.append(model_path)
            return cls()

        def synthesize(self, text, syn_config=None):
            fake_synth_calls.append((text, syn_config))
            return iter([])  # empty so playback path no-ops

    _install_fake_piper(monkeypatch, FakePiperVoice)

    engine = tts_mod.SpeechEngine(
        enabled=True,
        voice_en="en_US-amy-medium",
        voice_pl="pl_PL-darkman-medium",
    )
    try:
        engine.say("Cześć, słychać mnie?", lang="pl")
        deadline = time.time() + 1.5
        while time.time() < deadline:
            if fake_synth_calls:
                break
            time.sleep(0.02)
        assert fake_voice_load_calls, "PiperVoice.load was not called"
        assert "pl_PL-darkman-medium" in fake_voice_load_calls[0]
        assert fake_synth_calls and fake_synth_calls[0][0] == "Cześć, słychać mnie?"
    finally:
        engine.shutdown()


def test_say_auto_lang_picks_polish_for_polish_text(monkeypatch, tmp_path):
    monkeypatch.setattr(tts_mod, "_PIPER_DATA_DIR", tmp_path)
    for vid in ("en_US-amy-medium", "pl_PL-darkman-medium"):
        (tmp_path / f"{vid}.onnx").touch()
        (tmp_path / f"{vid}.onnx.json").touch()

    loaded: list[str] = []

    class FakePiperVoice:
        config = _FakeConfig()

        @classmethod
        def load(cls, model_path, config_path=None):
            loaded.append(model_path)
            return cls()

        def synthesize(self, text, syn_config=None):
            return iter([])

    _install_fake_piper(monkeypatch, FakePiperVoice)

    engine = tts_mod.SpeechEngine(
        enabled=True,
        voice_en="en_US-amy-medium",
        voice_pl="pl_PL-darkman-medium",
    )
    try:
        engine.say("Małomiasteczkowy", lang="auto")
        deadline = time.time() + 1.0
        while time.time() < deadline:
            if loaded:
                break
            time.sleep(0.02)
        assert loaded and "pl_PL-darkman-medium" in loaded[0]
    finally:
        engine.shutdown()


def test_cancel_clears_queue(monkeypatch, tmp_path):
    monkeypatch.setattr(tts_mod, "_PIPER_DATA_DIR", tmp_path)
    # Don't stage voices so the worker fails fast on each job — fine,
    # we're testing queue drain semantics, not synthesis.

    engine = tts_mod.SpeechEngine(
        enabled=True,
        voice_en="en_US-amy-medium",
        voice_pl="pl_PL-darkman-medium",
    )
    try:
        # Block the worker by pretending the queue is held while we
        # enqueue. Simplest: enqueue several, cancel, assert queue is
        # empty (cancel drains it).
        engine._queue.put(("one", "en"))
        engine._queue.put(("two", "en"))
        engine.cancel()
        # After cancel, the queue should be empty (drained synchronously)
        assert engine._queue.empty()
    finally:
        engine.shutdown()


def test_say_empty_text_no_op():
    engine = tts_mod.SpeechEngine(
        enabled=True,
        voice_en="en_US-amy-medium",
        voice_pl="pl_PL-darkman-medium",
    )
    try:
        engine.say("")
        engine.say(None)  # type: ignore[arg-type]
        # Queue stays empty
        time.sleep(0.05)
        assert engine._queue.empty()
    finally:
        engine.shutdown()
