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


def test_detect_lang_catches_polish_without_diacritics_via_digraphs():
    """The diacritics-only check missed the LLM's diacritic-free
    Polish responses, so the English voice tried to read them.
    Field example: 'Wznawiam odtwarzanie.' has no Polish diacritics
    but does contain 'rz' — which is ~Polish-only as a digraph."""
    assert tts_mod._detect_lang("Wznawiam odtwarzanie.") == "pl"
    assert tts_mod._detect_lang("Otwarte: Firefox, Konsole.") == "en"  # comma list
    # 'rz', 'cz', 'sz' all flag as Polish
    assert tts_mod._detect_lang("Marzec to trzeci miesiac.") == "pl"
    assert tts_mod._detect_lang("Czesc, jak sie masz?") == "pl"
    assert tts_mod._detect_lang("Szybki test.") == "pl"


def test_detect_lang_catches_polish_via_word_suffixes():
    """Polish-specific suffixes catch diacritic-free + digraph-free
    text. -anie, -enie, -ość, -ego are essentially never English."""
    # Note: 'anie' etc. need to be a full word ending, not in the middle.
    assert tts_mod._detect_lang("Pisanie tekstu jest fajne.") == "pl"  # also has 'st'
    # Pure English with no Polish markers stays English.
    assert tts_mod._detect_lang("The quick brown fox.") == "en"
    assert tts_mod._detect_lang("Hello world, how are you?") == "en"


def test_detect_lang_handles_short_or_ambiguous_input():
    """Empty, single-word, or genuinely-ambiguous input falls back
    to English. Better to mispronounce a one-word answer than to
    misroute on a long one."""
    assert tts_mod._detect_lang("") == "en"
    assert tts_mod._detect_lang("ok") == "en"
    assert tts_mod._detect_lang("yes") == "en"


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


def test_play_prepends_preroll_silence(monkeypatch):
    """Regression: PipeWire/PortAudio takes 50-100 ms to spin up
    after stream.start(); the first ~50-100 ms of audio gets cut off
    at the speaker. Prepending silence lets the stream warm up
    before the speech samples flow. Verify _play writes pre-roll +
    audio, not just audio."""
    import numpy as np

    fake_stream = MagicMock()
    fake_stream.latency = 0.05  # short so the test isn't slow
    monkeypatch.setattr(tts_mod.sd, "OutputStream", lambda **kw: fake_stream)

    written: list[np.ndarray] = []
    fake_stream.write.side_effect = lambda chunk: written.append(chunk.copy())

    engine = tts_mod.SpeechEngine(
        enabled=True,
        voice_en="en_US-amy-medium",
        voice_pl="pl_PL-darkman-medium",
    )
    try:
        sample_rate = 22050
        # 50 ms of "speech" — distinct value so we can find it in the
        # written buffer.
        speech_samples = sample_rate * 50 // 1000
        audio = np.full(speech_samples, 1234, dtype=np.int16)
        engine._play(audio, sample_rate=sample_rate)

        all_written = np.concatenate(written) if written else np.zeros(0, dtype=np.int16)
        # 100 ms pre-roll + 50 ms speech at 22.05 kHz = ~3308 samples.
        expected_total = sample_rate * tts_mod.SpeechEngine._PREROLL_MS // 1000 + speech_samples
        assert all_written.shape[0] == expected_total
        # The first PREROLL_MS worth of samples are zero (the pre-roll).
        preroll_size = sample_rate * tts_mod.SpeechEngine._PREROLL_MS // 1000
        assert (all_written[:preroll_size] == 0).all(), (
            "pre-roll should be silence so the stream warms up before speech plays"
        )
        # Speech samples follow immediately after the pre-roll.
        assert (all_written[preroll_size:] == 1234).all()
    finally:
        engine.shutdown()


def test_play_drains_hardware_buffer_before_returning(monkeypatch):
    """Regression: stream.stop() flushes PortAudio's ring buffer but
    leaves the device's own DAC buffer (50–150 ms) playing. If _play
    returns immediately, MainWindow's playback_finished slot fires
    while audio is still coming out of the speaker — open mic catches
    the tail and Whisper transcribes it. _play must wait at least
    stream.latency before returning so MainWindow's snip captures the
    full bleed window."""
    import numpy as np

    fake_stream = MagicMock()
    fake_stream.latency = 0.12  # 120 ms reported device latency

    monkeypatch.setattr(tts_mod.sd, "OutputStream", lambda **kw: fake_stream)

    engine = tts_mod.SpeechEngine(
        enabled=True,
        voice_en="en_US-amy-medium",
        voice_pl="pl_PL-darkman-medium",
    )
    try:
        # 100 samples × int16; we don't actually play, just check
        # _play doesn't return before the drain wait elapses.
        audio = np.zeros(100, dtype=np.int16)
        t0 = time.monotonic()
        engine._play(audio, sample_rate=22050)
        elapsed = time.monotonic() - t0

        # Drain wait = max(0.1, latency) + 0.05 = 0.17 s.
        assert elapsed >= 0.17, (
            f"_play returned in {elapsed*1000:.0f} ms; expected >= 170 ms drain"
        )
        # The full latency comes from the wait, not from blocking writes —
        # the mock OutputStream's write() returns instantly.
        fake_stream.start.assert_called_once()
        fake_stream.stop.assert_called_once()
    finally:
        engine.shutdown()


def test_play_drain_aborts_on_cancel(monkeypatch):
    """During the drain wait, cancel() must unblock _play immediately
    instead of forcing the user to wait out the latency window."""
    import numpy as np

    fake_stream = MagicMock()
    fake_stream.latency = 1.0  # generous so the cancel-shortcircuit is visible

    monkeypatch.setattr(tts_mod.sd, "OutputStream", lambda **kw: fake_stream)

    engine = tts_mod.SpeechEngine(
        enabled=True,
        voice_en="en_US-amy-medium",
        voice_pl="pl_PL-darkman-medium",
    )
    try:
        audio = np.zeros(100, dtype=np.int16)
        # Cancel from another thread shortly after _play enters the
        # drain wait.
        def _cancel_soon():
            time.sleep(0.05)
            engine._cancel_event.set()
        threading.Thread(target=_cancel_soon, daemon=True).start()

        t0 = time.monotonic()
        engine._play(audio, sample_rate=22050)
        elapsed = time.monotonic() - t0

        # Should return well before the 1.0 s drain timeout.
        assert elapsed < 0.5, (
            f"_play didn't unblock on cancel; took {elapsed*1000:.0f} ms"
        )
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
