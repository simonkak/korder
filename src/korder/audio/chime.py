"""Pre-recorded start-of-listening chime.

Loaded from chime.opus shipped alongside this module. Plays through
sounddevice as the audible "go" signal that mic just opened. Mic
capture is deferred until the chime's perceptually-loud window
finishes — the file's tail (where amplitude has decayed below ASR
threshold) plays during early recording without polluting transcripts.

Why a baked file vs runtime synthesis: the project picked a specific
sound for character; preserving that aesthetic across versions is
more valuable than synthesis flexibility. The .opus file is small
(~5 KB), single-channel, 48 kHz — small enough not to fuss about
in the wheel.

Decoding goes through ``soundfile`` (libsndfile binding). libsndfile
1.0.29+ supports Opus natively; on Arch / CachyOS / most current
distros that's covered by the system ``libsndfile`` package which
``soundfile`` discovers automatically.
"""
from __future__ import annotations
import logging
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)


# Asset shipped with the package. Use the importlib.resources idiom
# in spirit (Path-relative-to-__file__) so installs from a wheel
# resolve correctly without extra package_data plumbing.
_CHIME_PATH = Path(__file__).parent / "chime.opus"

# How long the caller defers mic-open. Sized to the chime's own
# envelope (measured: peak energy in the first ~250 ms, RMS below
# -38 dBFS by 400 ms). The remaining ~270 ms tail plays during
# early recording but is sub-bleed amplitude — combined with the
# ducker's -10 dB system-volume drop and typical speaker→mic
# coupling loss (~20-30 dB), Whisper never sees it.
_DEFERRAL_MS = 400


def build_start_chime() -> tuple[np.ndarray, int, int]:
    """Load the chime once and cache. Returns
    (audio: float32 mono, sample_rate, duration_ms_for_caller_deferral).

    The third element is the DEFERRAL window the caller waits before
    opening the mic — not the audio's full length. Raises ImportError
    if soundfile isn't available, or any decoder error from soundfile
    (FileNotFoundError, RuntimeError) if the asset is missing or
    unreadable. The caller (play_start_chime) catches and returns 0,
    which downgrades to "no chime" rather than crashing dictation.
    """
    cached = _cache.get("buf")
    if cached is not None:
        return cached
    # Lazy import — if soundfile or libsndfile is missing, the user
    # gets a one-time warning and the chime path no-ops; the rest of
    # Korder still works.
    import soundfile as sf
    audio, sample_rate = sf.read(
        str(_CHIME_PATH), dtype="float32", always_2d=False,
    )
    if audio.ndim > 1:
        # Stereo → mono via average. Keeps the spatial character
        # neutral; for a UI chime, mono is correct.
        audio = audio.mean(axis=1)
    audio = np.ascontiguousarray(audio, dtype=np.float32)
    out = (audio, int(sample_rate), _DEFERRAL_MS)
    _cache["buf"] = out
    log.debug(
        "chime: loaded %s (%d samples @ %d Hz)",
        _CHIME_PATH.name, audio.shape[0], sample_rate,
    )
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

    Returns the deferral window in milliseconds: how long the caller
    should wait before opening the mic. Caller schedules its own
    follow-up work (typically `recorder.start()`) via
    QTimer.singleShot using this duration plus a small slack —
    keeps Qt-loop concerns out of the audio module.

    On any failure (no chime asset, soundfile missing, no audio
    device, busy stream), logs at warning level and returns 0 —
    caller should treat that as "skip the chime" and proceed
    immediately with mic open.
    """
    try:
        audio, sample_rate, duration_ms = build_start_chime()
    except (ImportError, FileNotFoundError, RuntimeError) as e:
        log.warning("chime: load failed: %s — proceeding without chime", e)
        return 0
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
