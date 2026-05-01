from __future__ import annotations
from typing import Callable
import numpy as np
import sounddevice as sd


class MicRecorder:
    """Push-to-talk recorder. start() begins capture, stop() returns mono float32 PCM."""

    def __init__(self, sample_rate: int = 16000, device: str | None = None, gain: float = 1.0):
        self.sample_rate = sample_rate
        self.device = device or None
        self.gain = float(gain)
        self._stream: sd.InputStream | None = None
        self._chunks: list[np.ndarray] = []
        # Optional hooks called immediately before start() opens the stream
        # and immediately after stop() closes it. Used by the Bluetooth
        # integration to flip the BT card to HFP for the duration of a
        # recording, then back to A2DP. None = no-op.
        self.pre_start: Callable[[], None] | None = None
        self.post_stop: Callable[[], None] | None = None

    def set_device(self, device: str | None) -> None:
        """Re-point the recorder at a different input. Cannot be called while
        a recording is in progress — the next start() will pick up the new
        device.
        """
        if self._stream is not None:
            raise RuntimeError("set_device() during active recording")
        self.device = device or None

    def _callback(self, indata, frames, time_info, status):
        chunk = indata[:, 0].copy()
        if self.gain != 1.0:
            chunk *= self.gain
        self._chunks.append(chunk)

    def start(self) -> None:
        self._chunks = []
        if self.pre_start is not None:
            try:
                self.pre_start()
            except Exception:
                # Hook failures must not block recording — fall through and
                # let sounddevice raise if the device is genuinely missing.
                pass
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> np.ndarray:
        if self._stream is None:
            return np.zeros(0, dtype=np.float32)
        self._stream.stop()
        self._stream.close()
        self._stream = None
        if self.post_stop is not None:
            try:
                self.post_stop()
            except Exception:
                pass
        if not self._chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self._chunks)

    def snapshot(self) -> np.ndarray:
        """Return a copy of the audio captured so far without stopping the stream."""
        chunks = list(self._chunks)
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks)

    @property
    def is_recording(self) -> bool:
        return self._stream is not None
