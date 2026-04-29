from __future__ import annotations
import numpy as np
import sounddevice as sd


class MicRecorder:
    """Push-to-talk recorder. start() begins capture, stop() returns mono float32 PCM."""

    def __init__(self, sample_rate: int = 16000, device: str | None = None):
        self.sample_rate = sample_rate
        self.device = device or None
        self._stream: sd.InputStream | None = None
        self._chunks: list[np.ndarray] = []

    def _callback(self, indata, frames, time_info, status):
        self._chunks.append(indata[:, 0].copy())

    def start(self) -> None:
        self._chunks = []
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
