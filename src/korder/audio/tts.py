"""Text-to-speech engine wrapper.

v1 backend: Piper (https://github.com/OHF-Voice/piper1-gpl). Neural,
ONNX-based, real-time on CPU; same ONNX runtime the wake-word extra
already pulls in. Voice models auto-download to ~/.local/share/piper
on first use (or pre-fetch with `python -m piper.download_voices
VOICE_ID`).

Threading model: a single worker thread owns the synthesis + playback
loop. ``say(text, lang)`` enqueues a job and returns immediately; the
worker pops jobs FIFO, synthesizes via PiperVoice, plays the audio
through sounddevice. ``cancel()`` clears the queue and aborts any
in-flight synthesis or playback within ~50 ms.

Voice caching: PiperVoice models are loaded lazily per-language on
first ``say`` and kept resident — model load is ~50 ms but synthesis
is ~100–400 ms, so paying load once is the right tradeoff for a
session.

If piper-tts isn't installed, this module imports successfully but
``SpeechEngine.is_available()`` returns False and ``say()`` no-ops
with a one-time warning. That keeps Korder functional for users who
opt out via `uv sync` (no `--extra tts`).
"""
from __future__ import annotations
import logging
import queue
import threading
from pathlib import Path
from typing import Any

import numpy as np
import sounddevice as sd
from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)


# Piper-voices ship in language-folders under ~/.local/share/piper.
# The voice ID format is `<locale>-<voice>-<quality>`; the model file
# is `<voice_id>.onnx` and config is `<voice_id>.onnx.json`.
_PIPER_DATA_DIR = Path.home() / ".local" / "share" / "piper"


def _voice_files(voice_id: str) -> tuple[Path, Path] | None:
    """Locate a downloaded voice's .onnx + .onnx.json by id. Searches
    common Piper data layouts; returns None when the voice isn't on
    disk yet (caller should download or fall back)."""
    if not voice_id:
        return None
    # Direct (modern layout): ~/.local/share/piper/<voice_id>.onnx
    direct = _PIPER_DATA_DIR / f"{voice_id}.onnx"
    direct_cfg = _PIPER_DATA_DIR / f"{voice_id}.onnx.json"
    if direct.is_file() and direct_cfg.is_file():
        return direct, direct_cfg
    # Per-voice subdir (older layout): ~/.local/share/piper/<voice_id>/<voice_id>.onnx
    sub = _PIPER_DATA_DIR / voice_id / f"{voice_id}.onnx"
    sub_cfg = _PIPER_DATA_DIR / voice_id / f"{voice_id}.onnx.json"
    if sub.is_file() and sub_cfg.is_file():
        return sub, sub_cfg
    return None


def _detect_lang(text: str) -> str:
    """Two-state heuristic mirroring actions/now_playing.py: 'pl' if
    Polish-only diacritics are present, else 'en'."""
    if any(ch in text for ch in "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ"):
        return "pl"
    return "en"


class SpeechEngine(QObject):
    """Thread-safe TTS facade. Constructor doesn't block — voice load
    is deferred to the first ``say`` per language. Set ``enabled=False``
    in the constructor to make every method a no-op (used when [tts]
    enabled = false in config)."""

    playback_finished = Signal()

    def __init__(
        self,
        *,
        enabled: bool,
        voice_en: str,
        voice_pl: str,
        speed: float = 1.0,
    ):
        super().__init__()
        self._enabled = enabled
        self._voice_ids: dict[str, str] = {"en": voice_en, "pl": voice_pl}
        # Piper's length-scale is the inverse of perceived speed:
        # length_scale=1.0 → normal; <1 faster; >1 slower. Clamp to
        # sane range.
        self._length_scale = 1.0 / max(0.5, min(2.0, float(speed)))
        # Lazy voice cache. Populated on first say() per language.
        self._voices: dict[str, Any] = {}
        # FIFO of (text, lang) jobs. None is the sentinel for shutdown.
        self._queue: queue.Queue[tuple[str, str] | None] = queue.Queue()
        # Set by cancel() — synthesis thread polls between chunks.
        self._cancel_event = threading.Event()
        # Track the currently-playing OutputStream so cancel can stop
        # blocking playback mid-buffer.
        self._stream_lock = threading.Lock()
        self._current_stream: sd.OutputStream | None = None
        # Worker thread. Daemon so it doesn't keep the process alive.
        self._worker: threading.Thread | None = None
        if self._enabled:
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="korder-tts",
                daemon=True,
            )
            self._worker.start()
        # Whether piper-tts is importable. Checked lazily so the
        # constructor stays cheap and the failure surfaces with a
        # narration only when the user actually triggers a speak.
        self._import_warned = False

    def is_available(self) -> bool:
        """Returns True iff the engine is enabled AND piper-tts is
        importable. Used by callers that want to decide whether to
        emit a speakable_response at all."""
        if not self._enabled:
            return False
        return self._import_piper_voice() is not None

    def voices_available(self, lang: str) -> list[str]:
        """List downloaded Piper voices for the given language (e.g.
        'en', 'pl'). Reads from ~/.local/share/piper. Empty list when
        nothing's downloaded yet — Settings UI can offer a manual
        entry field for that case."""
        if not _PIPER_DATA_DIR.is_dir():
            return []
        prefix_short = lang.lower() + "_"
        out: list[str] = []
        for entry in _PIPER_DATA_DIR.iterdir():
            stem = entry.stem
            if entry.is_file() and entry.suffix == ".onnx" and stem.lower().startswith(prefix_short):
                out.append(stem)
            elif entry.is_dir() and entry.name.lower().startswith(prefix_short):
                # Per-voice subdir layout
                model = entry / f"{entry.name}.onnx"
                if model.is_file():
                    out.append(entry.name)
        return sorted(set(out))

    def say(self, text: str, lang: str = "auto") -> None:
        """Queue an utterance for synthesis + playback. Returns
        immediately. lang='auto' picks PL or EN by diacritic
        heuristic."""
        if not self._enabled or not text:
            return
        if lang == "auto":
            lang = _detect_lang(text)
        if lang not in self._voice_ids:
            log.warning("tts: unknown language %r — falling back to 'en'", lang)
            lang = "en"
        # Reset cancel state in case a previous cancel left it set.
        self._cancel_event.clear()
        self._queue.put((text, lang))

    def cancel(self) -> None:
        """Drop any queued utterances and stop in-flight synthesis +
        playback. Idempotent — calling when nothing is playing is a
        no-op."""
        if not self._enabled:
            return
        self._cancel_event.set()
        # Drain the queue.
        try:
            while True:
                self._queue.get_nowait()
                self._queue.task_done()
        except queue.Empty:
            pass
        # Stop any blocking playback.
        with self._stream_lock:
            stream = self._current_stream
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass

    def is_playing(self) -> bool:
        """Returns True iff a synthesis or playback is currently in
        flight. Used by the UI to coordinate cancel decisions."""
        if not self._enabled:
            return False
        with self._stream_lock:
            return self._current_stream is not None

    def shutdown(self) -> None:
        """Stop the worker thread cleanly. Called at app exit."""
        if not self._enabled or self._worker is None:
            return
        self.cancel()
        self._queue.put(None)  # sentinel
        self._worker.join(timeout=2.0)

    # ---- internals ----

    def _import_piper_voice(self):
        """Lazy import. Returns the PiperVoice class on success, None
        when piper-tts isn't installed. Warns once."""
        try:
            from piper import PiperVoice  # type: ignore
            return PiperVoice
        except ImportError:
            if not self._import_warned:
                log.warning(
                    "tts: piper-tts not installed — install with `uv sync --extra tts`"
                )
                self._import_warned = True
            return None

    def _load_voice(self, lang: str):
        """Load + cache a Piper voice for the given language. Returns
        the voice object or None on any failure (missing files, etc.)."""
        if lang in self._voices:
            return self._voices[lang]
        PiperVoice = self._import_piper_voice()
        if PiperVoice is None:
            return None
        voice_id = self._voice_ids.get(lang)
        if not voice_id:
            return None
        files = _voice_files(voice_id)
        if files is None:
            log.error(
                "tts: voice %r not on disk under %s — fetch with "
                "`python -m piper.download_voices %s`",
                voice_id, _PIPER_DATA_DIR, voice_id,
            )
            return None
        model_path, config_path = files
        try:
            voice = PiperVoice.load(str(model_path), config_path=str(config_path))
        except Exception as e:
            log.error("tts: failed to load voice %r: %s", voice_id, e)
            return None
        self._voices[lang] = voice
        log.info("tts: loaded voice %r for %r", voice_id, lang)
        return voice

    def _synthesize_to_array(self, voice, text: str) -> tuple[np.ndarray, int] | None:
        """Run Piper synthesis and assemble a single int16 numpy array
        for sounddevice. Returns (audio, sample_rate) or None on
        failure / mid-synth cancel.

        Piper 1.2's synthesize() takes a SynthesisConfig object
        (not kwargs); sample rate lives on voice.config, not on
        each AudioChunk. Older Piper signatures are accommodated
        via try-except since some users may have older builds."""
        # Build the synthesis config — length_scale is INVERSE of
        # speed (smaller = faster). voice.config holds the model's
        # default; we override only the speed knob.
        try:
            from piper.config import SynthesisConfig  # type: ignore
            syn_config = SynthesisConfig(length_scale=self._length_scale)
        except ImportError:
            syn_config = None

        try:
            sample_rate = int(getattr(voice.config, "sample_rate", 22050))
            chunks: list[np.ndarray] = []
            # Try the modern API first; fall back to legacy kwargs
            # if SynthesisConfig isn't available or rejected.
            if syn_config is not None:
                synth_iter = voice.synthesize(text, syn_config=syn_config)
            else:
                synth_iter = voice.synthesize(text)
            for chunk in synth_iter:
                if self._cancel_event.is_set():
                    return None
                arr = getattr(chunk, "audio_int16_array", None)
                if arr is None:
                    # Fallback: AudioChunk.audio_int16_bytes → ndarray
                    raw = getattr(chunk, "audio_int16_bytes", None)
                    if raw is None:
                        continue
                    arr = np.frombuffer(raw, dtype=np.int16)
                chunks.append(np.asarray(arr, dtype=np.int16))
        except Exception as e:
            log.error("tts: synthesis failed: %s", e)
            return None
        if not chunks:
            return None
        audio = np.concatenate(chunks)
        return audio, sample_rate

    def _play(self, audio: np.ndarray, sample_rate: int) -> None:
        """Blocking playback through sounddevice. Sets
        _current_stream so cancel() can abort. Mono int16."""
        try:
            stream = sd.OutputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="int16",
            )
        except Exception as e:
            log.error("tts: OutputStream init failed: %s", e)
            return
        with self._stream_lock:
            self._current_stream = stream
        try:
            stream.start()
            # Write in modest chunks so cancel-via-abort lands within
            # one chunk's worth of latency (~25 ms for 1024 samples
            # @22.05 kHz).
            CHUNK = 1024
            for i in range(0, audio.shape[0], CHUNK):
                if self._cancel_event.is_set():
                    break
                stream.write(audio[i:i + CHUNK].reshape(-1, 1))
        except Exception as e:
            log.error("tts: playback error: %s", e)
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            with self._stream_lock:
                self._current_stream = None

    def _worker_loop(self) -> None:
        """Single thread that owns the synth + playback pipeline.
        Pulls (text, lang) jobs FIFO, runs synth, plays audio,
        repeats. None sentinel = shutdown."""
        while True:
            job = self._queue.get()
            if job is None:
                break
            try:
                text, lang = job
                if self._cancel_event.is_set():
                    self._cancel_event.clear()
                    continue
                voice = self._load_voice(lang)
                if voice is None:
                    continue
                synth = self._synthesize_to_array(voice, text)
                if synth is None or self._cancel_event.is_set():
                    self._cancel_event.clear()
                    continue
                audio, sample_rate = synth
                self._play(audio, sample_rate)
                self.playback_finished.emit()
            except Exception as e:
                log.error("tts: worker error: %s", e)
            finally:
                self._queue.task_done()
                # Reset cancel state for the next job.
                self._cancel_event.clear()
