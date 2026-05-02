"""Soft synthwave-bell start-of-listening chime.

A short bell-like cue synthesized from 5 partials with separate
exponential decay envelopes — gives the metallic-shimmer-and-mellow-
ringdown character of '80s FM-bell synth patches (Roland D-50,
DX7 "Tubular Bell" family). Plays through sounddevice as the
audible "go" signal that mic is open.

The transcription pipeline starts AFTER the chime's perceptually-loud
window finishes — but the bell's full ringdown extends past that
point at low amplitude (≤ -30 dBFS by then). Whisper doesn't pick
that tail up at typical mic gains, so we get the long bell character
without speaker-bleed pollution in transcripts.

Why synthesize instead of shipping a WAV:

  - One fewer asset in the repo.
  - No license-attribution hassle.
  - Trivial to retune — every constant below is a knob.

Constants are tuned by ear for "warm + brief + recognizable as 'go,'
not as 'error'". Frequencies cluster around C5 because that's a
neutral mid-pitch range — bells lower than ~400 Hz read as serious /
churchy; higher than ~2 kHz read as ping / alert.
"""
from __future__ import annotations
import logging
import threading

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)


_SAMPLE_RATE = 44100
# Total audio duration. Bell rings out longer than the deferral
# window — by the time _DURATION_FOR_DEFERRAL_S elapses, the
# remaining tail is below speaker-bleed threshold for Whisper.
_DURATION_S = 0.55
# How long the caller waits before opening the mic. The bell's full
# ringdown extends past this point (~330 ms of tail), but by t=0.28 s
# the partials have decayed to ~25 % of peak; combined with the
# ducker dropping system volume to 30 % at mic-open, speaker bleed
# at the input lands well below ASR threshold.
_DURATION_FOR_DEFERRAL_S = 0.28

# Fundamental: C5 (523.25 Hz). Pleasant mid-range; not too low
# (avoids "serious chime" connotation), not too high (avoids
# "alert ping" connotation).
_F0_HZ = 523.25

# Partial layout: (frequency_multiplier, peak_amplitude, decay_tau_s).
# Detuning the upper partials by fractions of a Hz creates the
# beating that makes synthwave bells sound "wide" / "warm" — same
# trick analog and FM bells use. Decay tau decreases with partial
# index so high frequencies fade first (the perceptual mellowing
# real bells exhibit during ringdown).
_PARTIALS = (
    # (freq_mult, amp, tau_s)
    (1.000, 1.00, 0.45),  # fundamental — slowest decay, anchors pitch
    (2.003, 0.55, 0.30),  # octave, slightly sharp for chorus shimmer
    (2.998, 0.30, 0.20),  # twelfth (octave + fifth), slightly flat
    (4.006, 0.18, 0.13),  # double octave, tiny detune
    (5.997, 0.10, 0.08),  # tritave, fastest decay (mellowing tail)
)

_PEAK_AMPLITUDE = 0.22   # ≈ -13 dBFS — softer than the old chime
_ATTACK_S = 0.008        # gentle attack: 8 ms — enough to avoid click,
                         # short enough to feel responsive


def build_start_chime() -> tuple[np.ndarray, int, int]:
    """Synthesize the bell once and cache. Returns
    (audio: float32 mono, sample_rate, duration_ms_for_caller_deferral).

    Note the third element is the DEFERRAL window, not the audio
    length — the audio is ~550 ms total (bell ringdown), but the
    caller only waits ~220 ms before opening the mic. The remaining
    ~330 ms of tail plays during early recording at sub-bleed
    amplitude and Whisper ignores it.
    """
    cached = _cache.get("buf")
    if cached is not None:
        return cached
    n = int(_SAMPLE_RATE * _DURATION_S)
    t = np.linspace(0.0, _DURATION_S, n, endpoint=False, dtype=np.float32)

    # Sum the partials, each with its own exp-decay envelope.
    audio = np.zeros(n, dtype=np.float32)
    for freq_mult, amp, tau in _PARTIALS:
        env = np.exp(-t / tau).astype(np.float32)
        audio += amp * env * np.sin(2 * np.pi * (_F0_HZ * freq_mult) * t)

    # Soft attack: linear fade-in to avoid the speaker pop that
    # full-amplitude sample 0 would produce.
    attack_n = int(_ATTACK_S * _SAMPLE_RATE)
    if attack_n > 0:
        attack = np.ones(n, dtype=np.float32)
        attack[:attack_n] = np.linspace(0.0, 1.0, attack_n, dtype=np.float32)
        audio = audio * attack

    # Normalize to peak then scale to the documented headroom.
    # Without normalization the partial-sum's peak depends on phase
    # alignment at sample 0 (~1.6× the largest single-partial amp),
    # which would clip on devices that don't soft-clip.
    peak = float(np.max(np.abs(audio)))
    if peak > 0:
        audio = audio * (_PEAK_AMPLITUDE / peak)

    deferral_ms = int(_DURATION_FOR_DEFERRAL_S * 1000)
    out = (audio.astype(np.float32), _SAMPLE_RATE, deferral_ms)
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
