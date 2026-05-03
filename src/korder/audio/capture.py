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
import logging
import threading
import time
from typing import Callable

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)


FrameCallback = Callable[[np.ndarray], None]


# PortAudio's RT-thread init occasionally times out when PipeWire/
# WirePlumber are still settling — typical right after boot or after
# pipewire restart. The error is paTimedOut (-9987). It's transient:
# a 250–500 ms wait usually clears it. We retry the open up to 4
# times with backoff before giving up; total worst case ~3.5s, which
# is acceptable for a one-shot startup cost.
_PA_OPEN_RETRY_DELAYS_S = (0.25, 0.5, 1.0, 1.75)


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
                log.error("mic subscriber error: %s", e)

    def subscribe(self, fn: FrameCallback) -> None:
        """Attach a frame consumer. Opens the audio stream on the first
        subscriber. The same callable can be subscribed multiple times
        and will then receive each frame multiple times — pair every
        subscribe with a matching unsubscribe.

        Raises sd.PortAudioError if the audio engine fails to come up
        even after retrying — this is rare in steady state and the
        caller surfaces it as 'mic error' to the user."""
        opening_first_stream = False
        with self._lock:
            self._subscribers.append(fn)
            if self._stream is None:
                opening_first_stream = True
        # PortAudio init and start happen outside the lock — they can
        # be slow (and on a cold pipewire come back paTimedOut a few
        # times). Constructing the InputStream before holding the
        # ref means a failed open can be retried cleanly without
        # leaving self._stream pointing at a half-initialized object.
        if opening_first_stream:
            try:
                stream = self._open_stream_with_retry()
            except Exception:
                # Roll back the subscriber so the next subscribe call
                # (or a retry from the caller) starts from a clean
                # state instead of accumulating a phantom subscription
                # that the audio callback will eventually call into.
                with self._lock:
                    try:
                        self._subscribers.remove(fn)
                    except ValueError:
                        pass
                raise
            with self._lock:
                self._stream = stream

    def _open_stream_with_retry(self) -> sd.InputStream:
        """Construct + start an InputStream, retrying on paTimedOut.

        PipeWire / WirePlumber occasionally aren't ready to spin up
        an RT thread when Korder launches right after boot or after
        a pipewire restart. The error surfaces as paTimedOut from
        PaUnixThread_New. A short backoff usually clears it on the
        next attempt."""
        last_exc: Exception | None = None
        for delay in (0.0,) + _PA_OPEN_RETRY_DELAYS_S:
            if delay > 0:
                time.sleep(delay)
            try:
                stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="float32",
                    device=self.device,
                    callback=self._callback,
                )
            except Exception as e:
                last_exc = e
                if not self._is_transient_pa_error(e):
                    raise
                log.warning("mic: InputStream construct failed (transient): %s", e)
                continue
            try:
                stream.start()
                return stream
            except Exception as e:
                last_exc = e
                # Failed start leaks the stream; close it before
                # retrying so PortAudio doesn't hold the device.
                try:
                    stream.close()
                except Exception:
                    pass
                if not self._is_transient_pa_error(e):
                    raise
                log.warning("mic: InputStream start failed (transient, will retry): %s", e)
                continue
        # All retries exhausted.
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _is_transient_pa_error(e: Exception) -> bool:
        """Heuristic: paTimedOut (-9987) and 'wait timed out' are the
        flaky-startup symptoms we retry. Other PortAudio errors
        (device missing, format unsupported) are deterministic and
        retrying just delays the eventual failure."""
        msg = str(e).lower()
        return (
            "timed out" in msg
            or "-9987" in msg
            or "patimedout" in msg
        )

    def unsubscribe(self, fn: FrameCallback) -> None:
        """Remove the first matching subscription. Closes the stream if
        no subscribers remain. Calls with an unknown callable are a
        no-op so callers don't need to track subscription state."""
        stream_to_close: sd.InputStream | None = None
        with self._lock:
            try:
                self._subscribers.remove(fn)
            except ValueError:
                return
            if not self._subscribers and self._stream is not None:
                stream_to_close = self._stream
                self._stream = None
        # PortAudio's stop()/close() block until the current callback
        # finishes; the callback itself wants self._lock to snapshot
        # subscribers, so holding the lock here would deadlock both
        # threads. Drop it first, then close.
        if stream_to_close is not None:
            stream_to_close.stop()
            stream_to_close.close()

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
