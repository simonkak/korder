"""Soft start-of-listening chime.

A short, pleasant two-tone ascending pattern (A5 → E6, perfect fifth)
synthesized in numpy at module level. Plays through sounddevice as the
audible "go" signal that mic is open. The transcription pipeline
deliberately starts AFTER the chime finishes — otherwise Whisper would
hear the chime via speaker bleed and try to transcribe it.

Why synthesize instead of shipping a WAV:

  - One fewer asset in the repo.
  - No license-attribution hassle.
  - Trivial to retune if the default tones turn out grating after
    long use; the constants below are the only knobs.

The chime is short (200 ms) and quiet (-12 dB peak). At default
playback volume it's noticeable but not startling, and the fade-out
guarantees no click at the tail.
"""
from __future__ import annotations
import logging
import threading

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)


# Tonal layout. Two ascending notes blended sequentially, half the
# total duration each. A5 → E6 is the same interval as iPhone's
# old "Tri-tone" SMS alert and Mac's startup chime — friendly,
# culturally well-worn, doesn't sound like an error.
_FREQ_LOW_HZ = 880.0    # A5
_FREQ_HIGH_HZ = 1320.0  # E6 — perfect fifth above A5
_DURATION_S = 0.20
_SAMPLE_RATE = 44100
_PEAK_AMPLITUDE = 0.25  # ≈ -12 dBFS — present but not startling
_FADE_IN_S = 0.015
_FADE_OUT_S = 0.060     # longer fade-out kills any click at the tail


def build_start_chime() -> tuple[np.ndarray, int, int]:
    """Synthesize the chime once and cache. Returns
    (audio: float32 mono, sample_rate, duration_ms). The cache means
    repeated chimes don't re-synth — the buffer is ~35 KB."""
    cached = _cache.get("buf")
    if cached is not None:
        return cached
    n = int(_SAMPLE_RATE * _DURATION_S)
    t = np.linspace(0.0, _DURATION_S, n, endpoint=False, dtype=np.float32)
    half = n // 2
    tone_low = np.sin(2 * np.pi * _FREQ_LOW_HZ * t[:half])
    tone_high = np.sin(2 * np.pi * _FREQ_HIGH_HZ * t[half:])
    audio = np.concatenate([tone_low, tone_high]).astype(np.float32)
    # Envelope: linear fade in + fade out so the speaker cone doesn't
    # snap on the first sample (audible click otherwise).
    envelope = np.ones(n, dtype=np.float32)
    fade_in_n = int(_FADE_IN_S * _SAMPLE_RATE)
    fade_out_n = int(_FADE_OUT_S * _SAMPLE_RATE)
    if fade_in_n > 0:
        envelope[:fade_in_n] = np.linspace(0.0, 1.0, fade_in_n, dtype=np.float32)
    if fade_out_n > 0:
        envelope[-fade_out_n:] = np.linspace(1.0, 0.0, fade_out_n, dtype=np.float32)
    audio = audio * envelope * _PEAK_AMPLITUDE
    duration_ms = int(_DURATION_S * 1000)
    out = (audio, _SAMPLE_RATE, duration_ms)
    _cache["buf"] = out
    return out


_cache: dict[str, tuple[np.ndarray, int, int]] = {}


# Track of the most recent stream so cancel_chime() can abort
# specifically that one rather than calling sd.stop() (which would
# also kill any TTS playback in flight on the global stream).
_active_stream_lock = threading.Lock()
_active_stream: sd.OutputStream | None = None


def play_start_chime() -> int:
    """Play the chime asynchronously via a dedicated OutputStream
    (separate from sd.play's global default so cancel_chime can stop
    it without disturbing TTS or other audio).

    Returns the chime's duration in milliseconds. Caller schedules
    its own follow-up work (typically `recorder.start()`) via
    QTimer.singleShot using this duration plus a small slack —
    keeps Qt-loop concerns out of the audio module.

    On any failure to open / start the stream (no device, busy,
    sounddevice import quirk), logs at warning level and returns 0
    — caller should treat that as "skip the chime" and proceed
    immediately.
    """
    audio, sample_rate, duration_ms = build_start_chime()
    try:
        stream = sd.OutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
        )
        stream.start()
    except Exception as e:
        log.warning("chime: OutputStream open failed: %s — proceeding without chime", e)
        return 0
    with _active_stream_lock:
        global _active_stream
        _active_stream = stream

    def _writer():
        try:
            stream.write(audio.reshape(-1, 1))
        except Exception as e:
            log.debug("chime: write interrupted: %s", e)
        finally:
            with _active_stream_lock:
                global _active_stream
                if _active_stream is stream:
                    _active_stream = None
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    threading.Thread(target=_writer, daemon=True, name="korder-chime").start()
    return duration_ms


def cancel_chime() -> None:
    """Abort any in-flight chime playback. Used when the user
    cancels (Esc / 'cancel that') during the brief window between
    chime start and recorder start. No-op when nothing is playing."""
    with _active_stream_lock:
        global _active_stream
        stream = _active_stream
        _active_stream = None
    if stream is None:
        return
    try:
        stream.abort()
    except Exception:
        pass
    try:
        stream.close()
    except Exception:
        pass
