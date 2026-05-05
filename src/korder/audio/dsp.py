"""Tiny stateful DSP frontend for the capture path.

Exists because Whisper's encoder is sensitive to two things the raw
PortAudio buffer doesn't clean up:

1. Sub-speech-band energy (DC offset, mains hum, HVAC rumble) lands in
   the bottom mel bins and shifts the encoder's frame-level statistics.
2. Loud transients clip the float32 path and mangle nearby phonemes.

Each filter is per-recorder (kept on ``MicRecorder``) so state persists
across PortAudio callback chunks. A stateless biquad applied per chunk
introduces ringing at the chunk boundaries — the very low-frequency
artifact we're trying to filter out.
"""
from __future__ import annotations
import math

import numpy as np


class HighPassFilter:
    """2nd-order Butterworth high-pass biquad. Coefficients are computed
    once at construction; per-chunk apply is two MACs/sample.

    State (the two delay-line samples) carries across calls so chunk
    boundaries don't introduce edge transients."""

    def __init__(self, cutoff_hz: float = 80.0, sample_rate: int = 16000):
        self.cutoff_hz = float(cutoff_hz)
        self.sample_rate = int(sample_rate)
        # Bilinear-transform Butterworth HPF. q = 1/sqrt(2) gives a
        # maximally-flat passband (Butterworth response).
        w0 = 2.0 * math.pi * self.cutoff_hz / self.sample_rate
        cos_w0 = math.cos(w0)
        sin_w0 = math.sin(w0)
        q = 1.0 / math.sqrt(2.0)
        alpha = sin_w0 / (2.0 * q)
        b0 = (1.0 + cos_w0) / 2.0
        b1 = -(1.0 + cos_w0)
        b2 = (1.0 + cos_w0) / 2.0
        a0 = 1.0 + alpha
        a1 = -2.0 * cos_w0
        a2 = 1.0 - alpha
        # Normalize so a0 = 1.
        self._b0 = b0 / a0
        self._b1 = b1 / a0
        self._b2 = b2 / a0
        self._a1 = a1 / a0
        self._a2 = a2 / a0
        # Direct-Form-II Transposed state (two samples).
        self._z1 = 0.0
        self._z2 = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        """Filter one chunk. Returns a new ndarray (input untouched).

        Direct-Form-II Transposed: numerically stable for fixed coeffs
        and only needs two state words regardless of chunk length.
        Loops in pure Python over a list (tolist + list iteration is
        ~5x faster than per-sample numpy indexing) — at 16 kHz with
        sounddevice's typical 1024-sample chunks, the whole thing
        comes in well under 1 ms, leaving ample headroom in the 64 ms
        callback budget. A vectorized version requires scipy.signal,
        which isn't a hard dep for the dictation-only path."""
        if x.size == 0:
            return x
        b0, b1, b2, a1, a2 = self._b0, self._b1, self._b2, self._a1, self._a2
        z1, z2 = self._z1, self._z2
        samples = x.tolist()
        out: list[float] = [0.0] * len(samples)
        for n, xn in enumerate(samples):
            yn = b0 * xn + z1
            z1 = b1 * xn - a1 * yn + z2
            z2 = b2 * xn - a2 * yn
            out[n] = yn
        self._z1 = z1
        self._z2 = z2
        return np.asarray(out, dtype=x.dtype)

    def reset(self) -> None:
        """Zero the delay line. Call when the input stream restarts."""
        self._z1 = 0.0
        self._z2 = 0.0


class PeakLimiter:
    """Peak-aware gain. Multiplies by ``target_gain`` when headroom
    allows; throttles down when the boosted peak would clip near 1.0.

    A slow-following peak EMA prevents per-chunk pumping when one
    chunk happens to contain the loudest sample of an utterance — the
    next ~200 ms inherits the same attenuation rather than snapping
    back to full gain on chunk N+1.
    """

    def __init__(self, target_gain: float = 1.0, ceiling: float = 0.95, decay: float = 0.999):
        self.target_gain = float(target_gain)
        self.ceiling = float(ceiling)
        self.decay = float(decay)
        self._peak_ema = 0.0

    def process(self, chunk: np.ndarray) -> np.ndarray:
        if chunk.size == 0:
            return chunk
        peak = float(np.max(np.abs(chunk))) if chunk.size else 0.0
        # Slow-following peak: max(current_peak, decayed_previous).
        # Decay is per-chunk, not per-sample — tracking per-sample would
        # require another scalar loop and the chunk-rate decay is more
        # than fast enough for ASR.
        self._peak_ema = max(peak, self.decay * self._peak_ema)
        ref = self._peak_ema if self._peak_ema > 0.0 else peak
        if ref <= 0.0:
            return chunk
        if ref * self.target_gain > self.ceiling:
            scale = self.ceiling / ref
        else:
            scale = self.target_gain
        if scale != 1.0:
            chunk *= scale
        return chunk

    def reset(self) -> None:
        self._peak_ema = 0.0


def remove_dc(chunk: np.ndarray) -> np.ndarray:
    """Subtract the chunk mean in-place. Cheap defense against
    persistent DC bias from USB / Bluetooth / virtual mics that would
    otherwise occupy the bottom mel bin."""
    if chunk.size == 0:
        return chunk
    chunk -= float(chunk.mean())
    return chunk


def resample_to(audio: np.ndarray, src_rate: int, dst_rate: int = 16000) -> np.ndarray:
    """Resample with scipy.signal.resample_poly when available, else
    return the input untouched and let Whisper's internal resampler
    handle it.

    scipy is a transitive dep of the wake/tts extras, not a hard
    dep of the dictation path; this stays best-effort so a bare
    install doesn't fail. The intended use is the submit-time guard
    for users who configured a non-16 kHz sample_rate — most setups
    are already at 16 kHz and short-circuit on the equality check."""
    if src_rate == dst_rate or audio.size == 0:
        return audio
    try:
        from scipy.signal import resample_poly
    except ImportError:
        return audio
    from math import gcd
    g = gcd(int(src_rate), int(dst_rate))
    up = int(dst_rate) // g
    down = int(src_rate) // g
    return resample_poly(audio, up, down).astype(audio.dtype, copy=False)


def normalize_rms(audio: np.ndarray, target: float = 0.05, ceiling: float = 0.95) -> np.ndarray:
    """One-shot bounded RMS lift for quiet utterances. Only scales UP.

    Whisper's encoder is moderately level-invariant but the
    ``_has_speech_energy`` gate and the mel-bin log-floor both fail
    quiet on under-driven utterances. Lifting just the quiet ones
    gives the encoder a more typical operating point without ever
    pulling loud ones down (Whisper handles loud fine).

    Returns a new array if scaling happens, the input otherwise."""
    if audio.size == 0:
        return audio
    rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
    if rms <= 0.0 or rms >= target:
        return audio
    scale = target / rms
    boosted = audio * scale
    peak = float(np.max(np.abs(boosted))) if boosted.size else 0.0
    if peak > ceiling:
        boosted *= ceiling / peak
    return boosted
