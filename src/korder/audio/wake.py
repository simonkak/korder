"""Wake-word detection for hands-free activation (issue #1).

Subscribes to MicRecorder for raw audio frames, runs them through an
openwakeword ONNX model, and emits a Qt signal when the configured
phrase fires above the sensitivity threshold. The detector is inert
until ``start()`` is called and only consumes one MicRecorder
subscriber slot while running.

openwakeword is an optional dep (``uv sync --extra wake``). The import
is deferred to ``start()`` so users on the hotkey-only path never see
an ImportError at module load time, even if they don't have it
installed.
"""
from __future__ import annotations
import sys
import threading

import numpy as np
from PySide6.QtCore import QObject, Signal


class WakeWordDetector(QObject):
    detected = Signal()
    error = Signal(str)

    # openwakeword's recommended chunk size — 80 ms at 16 kHz. Smaller
    # chunks lower the wake-to-OSD latency floor; larger chunks lower
    # CPU overhead. 1280 samples is the catalog's training boundary.
    CHUNK_SAMPLES = 1280

    # Cooldown after a detection — suppresses repeat fires from a
    # single spoken phrase that bleeds across consecutive chunks.
    _COOLDOWN_S = 1.0

    def __init__(
        self,
        recorder,
        phrase: str = "hey_jarvis",
        sensitivity: float = 0.5,
        sample_rate: int = 16000,
    ):
        super().__init__()
        self._recorder = recorder
        self._phrase = phrase
        self._sensitivity = float(sensitivity)
        self._sample_rate = int(sample_rate)
        self._buffer: list[np.ndarray] = []
        self._running = False
        # Guards _buffer mutation; the audio callback runs off the Qt
        # thread, so accumulating frames needs to interleave safely
        # with start()/stop() from the UI side.
        self._lock = threading.Lock()
        self._model = None
        self._cooldown_frames = 0

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        """Load the model (if not already loaded) and begin listening.
        Raises ImportError if the openwakeword extra isn't installed."""
        if self._running:
            return
        if self._model is None:
            try:
                from openwakeword.model import Model
            except ImportError as e:
                raise ImportError(
                    "openwakeword is required for wake-word activation. "
                    "Install with `uv sync --extra wake`."
                ) from e
            try:
                self._model = Model(wakeword_models=[self._phrase])
            except Exception as e:
                raise RuntimeError(
                    f"failed to load wake-word model {self._phrase!r}: {e}"
                ) from e
        with self._lock:
            self._buffer.clear()
            self._cooldown_frames = 0
        self._running = True
        self._recorder.subscribe(self._on_frame)
        print(
            f"[korder] wake: listening for {self._phrase!r} "
            f"(sensitivity {self._sensitivity:.2f})",
            flush=True, file=sys.stderr,
        )

    def stop(self) -> None:
        """Stop listening. Releases the MicRecorder subscriber slot but
        keeps the loaded model around so a subsequent start() is fast."""
        if not self._running:
            return
        self._running = False
        try:
            self._recorder.unsubscribe(self._on_frame)
        except Exception:
            pass
        print("[korder] wake: stopped listening", flush=True, file=sys.stderr)

    def _on_frame(self, chunk: np.ndarray) -> None:
        """MicRecorder subscriber callback. Accumulates frames until a
        full CHUNK_SAMPLES window is available, then runs inference and
        emits ``detected`` if the phrase scores above sensitivity."""
        if not self._running or self._model is None:
            return
        # openwakeword wants int16 PCM at 16 kHz.
        ints = (np.clip(chunk, -1.0, 1.0) * 32767.0).astype(np.int16)
        with self._lock:
            self._buffer.append(ints)
            buffered = sum(b.shape[0] for b in self._buffer)
            if buffered < self.CHUNK_SAMPLES:
                return
            full = np.concatenate(self._buffer)
            consume = full[: self.CHUNK_SAMPLES]
            leftover = full[self.CHUNK_SAMPLES :]
            self._buffer = [leftover] if leftover.size else []
            cooldown = self._cooldown_frames
        # Run inference outside the lock so a slow predict() call
        # doesn't block other subscribers (or start()/stop()).
        try:
            preds = self._model.predict(consume)
        except Exception as e:
            self.error.emit(f"wake inference: {e}")
            return
        score = float(preds.get(self._phrase, 0.0))
        if cooldown > 0:
            with self._lock:
                # Re-check under the lock — start() may have reset us.
                if self._cooldown_frames > 0:
                    self._cooldown_frames -= 1
            return
        if score >= self._sensitivity:
            print(
                f"[korder] wake: {self._phrase!r} fired "
                f"(score={score:.3f} >= {self._sensitivity:.2f})",
                flush=True, file=sys.stderr,
            )
            cooldown_chunks = int(
                self._COOLDOWN_S * self._sample_rate / self.CHUNK_SAMPLES
            )
            with self._lock:
                self._cooldown_frames = cooldown_chunks
            self.detected.emit()
