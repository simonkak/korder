"""Audio capture engine with multi-subscriber fan-out.

The mic stream stays open as long as at least one subscriber is
attached. Subscribers receive mono float32 frames at sample_rate Hz
via a callback. The push-to-talk dictation flow is implemented as a
built-in subscriber that collects frames into a buffer; ``start()``
attaches it, ``stop()`` returns the buffer and detaches it. External
consumers (wake-word detector, VAD-on-tap, level meter, …) call
``subscribe()``/``unsubscribe()`` and run alongside without affecting
the dictation flow.
"""
from __future__ import annotations
import sys
import threading
from typing import Callable

import numpy as np
import sounddevice as sd


FrameCallback = Callable[[np.ndarray], None]


class MicRecorder:
    def __init__(self, sample_rate: int = 16000, device: str | None = None, gain: float = 1.0):
        self.sample_rate = sample_rate
        self.device = device or None
        self.gain = float(gain)
        self._stream: sd.InputStream | None = None
        # Subscribers attached via subscribe(). The dictation collector
        # joins the same list when start() is called.
        self._subscribers: list[FrameCallback] = []
        # Lock guards subscribe/unsubscribe so the audio callback never
        # iterates a list that's being mutated mid-frame.
        self._lock = threading.Lock()
        # Set to a list while a dictation session is active. None at
        # rest. Used by snapshot()/stop() to read the captured buffer
        # without exposing it to other subscribers.
        self._dictation_chunks: list[np.ndarray] | None = None

    def _callback(self, indata, frames, time_info, status):
        chunk = indata[:, 0].copy()
        if self.gain != 1.0:
            chunk *= self.gain
        # Snapshot the subscriber list under the lock so a concurrent
        # subscribe()/unsubscribe() can't corrupt iteration. Then call
        # outside the lock so a slow subscriber doesn't block the audio
        # thread on the next frame.
        with self._lock:
            subs = list(self._subscribers)
        for sub in subs:
            try:
                sub(chunk)
            except Exception as e:
                print(f"[korder] mic subscriber error: {e}", flush=True, file=sys.stderr)

    def subscribe(self, fn: FrameCallback) -> None:
        """Attach a frame consumer. Opens the audio stream on the first
        subscriber. The same callable can be subscribed multiple times
        and will then receive each frame multiple times — pair every
        subscribe with a matching unsubscribe."""
        with self._lock:
            self._subscribers.append(fn)
            if self._stream is None:
                self._stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="float32",
                    device=self.device,
                    callback=self._callback,
                )
                self._stream.start()

    def unsubscribe(self, fn: FrameCallback) -> None:
        """Remove the first matching subscription. Closes the stream if
        no subscribers remain. Calls with an unknown callable are a
        no-op so callers don't need to track subscription state."""
        with self._lock:
            try:
                self._subscribers.remove(fn)
            except ValueError:
                return
            if not self._subscribers and self._stream is not None:
                self._stream.stop()
                self._stream.close()
                self._stream = None

    def start(self) -> None:
        """Begin a push-to-talk dictation session. Installs the
        dictation collector as a subscriber; stop() returns its buffer."""
        if self._dictation_chunks is not None:
            return
        self._dictation_chunks = []
        self.subscribe(self._collect_dictation)

    def _collect_dictation(self, chunk: np.ndarray) -> None:
        if self._dictation_chunks is not None:
            self._dictation_chunks.append(chunk)

    def stop(self) -> np.ndarray:
        if self._dictation_chunks is None:
            return np.zeros(0, dtype=np.float32)
        chunks = self._dictation_chunks
        self._dictation_chunks = None
        self.unsubscribe(self._collect_dictation)
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks)

    def snapshot(self) -> np.ndarray:
        """Return a copy of the audio captured so far in the current
        dictation session, without stopping it."""
        if not self._dictation_chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(list(self._dictation_chunks))

    @property
    def is_recording(self) -> bool:
        return self._dictation_chunks is not None
