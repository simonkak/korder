"""Frontend DSP tests for the capture path."""
from __future__ import annotations
import numpy as np
import pytest

from korder.audio.dsp import (
    HighPassFilter,
    PeakLimiter,
    normalize_rms,
    remove_dc,
)


SR = 16000


def _tone(freq_hz: float, amplitude: float, duration_s: float) -> np.ndarray:
    n = int(duration_s * SR)
    t = np.arange(n) / SR
    return (amplitude * np.sin(2 * np.pi * freq_hz * t)).astype(np.float32)


def test_remove_dc_zeros_constant_input():
    chunk = np.full(1024, 0.3, dtype=np.float32)
    remove_dc(chunk)
    assert chunk.mean() == pytest.approx(0.0, abs=1e-6)


def test_remove_dc_handles_empty():
    empty = np.zeros(0, dtype=np.float32)
    remove_dc(empty)  # must not raise


def test_hpf_passes_speech_band_tone():
    """1 kHz sits well inside the passband — amplitude should round-trip
    near-unchanged after the filter settles."""
    sig = _tone(1000.0, 0.3, 1.0)
    hpf = HighPassFilter(cutoff_hz=80.0, sample_rate=SR)
    out = hpf.process(sig)
    settled = out[int(0.1 * SR):]  # skip startup transient
    assert np.abs(settled).max() == pytest.approx(0.3, abs=0.02)


def test_hpf_attenuates_subspeech_rumble():
    """50 Hz mains hum is well below the 80 Hz cutoff — should be
    measurably attenuated relative to the passband (where it'd survive)."""
    hum = _tone(50.0, 0.3, 1.0)
    hpf = HighPassFilter(cutoff_hz=80.0, sample_rate=SR)
    out = hpf.process(hum)
    settled = out[int(0.1 * SR):]
    assert np.abs(settled).max() < 0.2  # at least ~3.5 dB down


def test_hpf_strips_dc_completely_after_settling():
    dc_offset = np.full(SR, 0.5, dtype=np.float32)
    hpf = HighPassFilter(cutoff_hz=80.0, sample_rate=SR)
    out = hpf.process(dc_offset)
    settled = out[int(0.1 * SR):]
    assert abs(settled.mean()) < 1e-3


def test_hpf_state_persists_across_chunks():
    """Splitting a tone into chunks and filtering each through the same
    HPF instance must produce the same result as filtering the whole
    tone in one shot — that's what 'state persists across callbacks'
    means in practice."""
    sig = _tone(1000.0, 0.3, 0.5)
    hpf_whole = HighPassFilter(cutoff_hz=80.0, sample_rate=SR)
    whole = hpf_whole.process(sig.copy())

    hpf_chunked = HighPassFilter(cutoff_hz=80.0, sample_rate=SR)
    chunks = np.array_split(sig.copy(), 10)
    out_chunks = [hpf_chunked.process(c) for c in chunks]
    chunked = np.concatenate(out_chunks)

    np.testing.assert_allclose(whole, chunked, atol=1e-5)


def test_hpf_reset_clears_state():
    sig = _tone(1000.0, 0.3, 0.1)
    hpf = HighPassFilter(cutoff_hz=80.0, sample_rate=SR)
    hpf.process(sig.copy())
    assert hpf._z1 != 0.0 or hpf._z2 != 0.0
    hpf.reset()
    assert hpf._z1 == 0.0 and hpf._z2 == 0.0


def test_peak_limiter_applies_target_gain_with_headroom():
    chunk = np.full(160, 0.25, dtype=np.float32)
    lim = PeakLimiter(target_gain=2.0)
    lim.process(chunk)
    # 0.25 * 2 = 0.5, well below 0.95 ceiling.
    assert chunk.max() == pytest.approx(0.5, abs=1e-4)


def test_peak_limiter_caps_at_ceiling_when_boost_would_clip():
    chunk = np.full(160, 0.8, dtype=np.float32)
    lim = PeakLimiter(target_gain=2.0)
    lim.process(chunk)
    # Would be 1.6; limiter throttles to 0.95/0.8 ≈ 1.1875 -> 0.95.
    assert chunk.max() == pytest.approx(0.95, abs=1e-3)


def test_peak_limiter_slow_following_peak_avoids_pumping():
    """A loud chunk followed by a quiet one shouldn't snap the gain
    back to full on the very next chunk — the EMA-tracked peak holds
    the attenuation across chunks for a few hundred ms."""
    lim = PeakLimiter(target_gain=2.0)
    loud = np.full(160, 0.8, dtype=np.float32)
    quiet = np.full(160, 0.1, dtype=np.float32)
    lim.process(loud)
    lim.process(quiet)
    # Without slow-following peak, quiet * 2.0 = 0.2. With it, the
    # limiter still references the recent loud peak and keeps the
    # scale near the throttled value, well below 2x.
    assert quiet.max() < 0.18


def test_peak_limiter_handles_silence():
    chunk = np.zeros(160, dtype=np.float32)
    lim = PeakLimiter(target_gain=2.0)
    lim.process(chunk)  # must not divide by zero
    assert chunk.max() == 0.0


def test_normalize_rms_boosts_quiet_to_target():
    quiet = _tone(440.0, 0.01, 1.0)
    boosted = normalize_rms(quiet, target=0.05)
    rms = float(np.sqrt(np.mean(boosted ** 2)))
    assert rms == pytest.approx(0.05, abs=0.005)


def test_normalize_rms_leaves_loud_alone():
    loud = _tone(440.0, 0.5, 1.0)
    out = normalize_rms(loud, target=0.05)
    np.testing.assert_array_equal(out, loud)


def test_normalize_rms_handles_silence():
    silence = np.zeros(SR, dtype=np.float32)
    out = normalize_rms(silence)
    np.testing.assert_array_equal(out, silence)


def test_normalize_rms_caps_at_ceiling():
    """A tiny signal scaled aggressively can hit the ceiling — verify
    the post-scale clip kicks in instead of producing >1.0 samples."""
    # Tone whose peak is 4x its rms (sinusoid), set so the boost would
    # otherwise exceed 0.95.
    near_zero = _tone(440.0, 0.0001, 1.0)
    boosted = normalize_rms(near_zero, target=0.05, ceiling=0.95)
    assert np.abs(boosted).max() <= 0.95 + 1e-6
