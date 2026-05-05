from __future__ import annotations
import re
import threading
import numpy as np
import webrtcvad
from pywhispercpp.model import Model

_BRACKET_TAG = re.compile(r"\s*\[[^\]]*\]\s*")
# Whisper sometimes emits sound-event annotations as *markdown emphasis*
# instead of [brackets] — '*dźwięk*', '*music*', '*applause*'. They show
# up as text otherwise and the LLM parser then treats them as content.
_ASTERISK_TAG = re.compile(r"\s*\*[^*\n]+\*\s*")


class WhisperEngine:
    """Wraps pywhispercpp (whisper.cpp + Vulkan). Loads lazily on first transcribe()."""

    def __init__(
        self,
        model: str = "medium",
        language: str | None = None,
        initial_prompt: str | None = None,
        n_threads: int = 4,
    ):
        self.model_name = model
        self.language = language or None
        self.initial_prompt = initial_prompt or None
        self.n_threads = n_threads
        self._model: Model | None = None
        self._lock = threading.RLock()

    def _ensure_loaded(self) -> Model:
        with self._lock:
            if self._model is None:
                kwargs: dict = {
                    "print_progress": False,
                    "print_realtime": False,
                    "print_timestamps": False,
                    "n_threads": self.n_threads,
                    "translate": False,
                    "language": self.language or "auto",
                    "suppress_blank": True,
                }
                if self.initial_prompt:
                    kwargs["initial_prompt"] = self.initial_prompt
                self._model = Model(
                    self.model_name,
                    redirect_whispercpp_logs_to=None,
                    **kwargs,
                )
        return self._model

    def transcribe(self, audio: np.ndarray, initial_prompt: str | None = None) -> str:
        if audio.size == 0:
            return ""
        if not _has_speech_frames(audio):
            return ""
        with self._lock:
            m = self._ensure_loaded()
            # whisper.cpp's params object is mutated when transcribe()
            # receives kwargs, so we always pass an explicit prompt
            # (falling back to the static config one) — otherwise the
            # last per-call value would silently leak into subsequent
            # calls. Empty string and None are treated identically by
            # whisper.cpp.
            prompt = initial_prompt if initial_prompt is not None else (self.initial_prompt or "")
            segments = m.transcribe(audio, initial_prompt=prompt)
        text = " ".join(s.text.strip() for s in segments).strip()
        return _strip_annotations(text)


def _strip_annotations(text: str) -> str:
    """Remove Whisper's sound-event annotations — both bracketed
    ([noises], [music]) and asterisk-emphasis (*dźwięk*, *applause*).
    Some prompts and decoder paths produce one or the other; both
    shapes show up in real Korder output."""
    cleaned = _BRACKET_TAG.sub(" ", text)
    cleaned = _ASTERISK_TAG.sub(" ", cleaned).strip()
    return re.sub(r"\s+", " ", cleaned)


def _has_speech_frames(
    audio: np.ndarray,
    sample_rate: int = 16000,
    aggressiveness: int = 2,
    min_speech_ratio: float = 0.10,
    min_speech_ms: int = 150,
) -> bool:
    """Frame-level webrtcvad gate. Returns True iff at least
    ``min_speech_ratio`` of 30 ms frames are speech AND at least
    ``min_speech_ms`` of contiguous speech is present.

    Replaces the global RMS gate that let long mostly-quiet buffers
    through (a 5 s clip with one cough averages well under 0.005 RMS
    only if it's pin-drop quiet — anything with room tone or HVAC
    rumble passed the gate, then Whisper hallucinated 'Dziękuję' /
    'Thank you' on it).

    Uses a fresh Vad instance to avoid coupling with any other
    webrtcvad call site (the detector in MainWindow has adaptive
    state that shouldn't bias engine-level gating)."""
    if audio.size == 0:
        return False
    vad = webrtcvad.Vad(aggressiveness)
    frame_size = sample_rate * 30 // 1000
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
    n_frames = len(pcm) // frame_size
    if n_frames == 0:
        return False
    needed_streak = max(1, min_speech_ms // 30)
    speech_count = 0
    streak = 0
    has_streak = False
    for i in range(n_frames):
        frame = pcm[i * frame_size:(i + 1) * frame_size]
        if vad.is_speech(frame.tobytes(), sample_rate):
            speech_count += 1
            streak += 1
            if streak >= needed_streak:
                has_streak = True
        else:
            streak = 0
    return has_streak and (speech_count / n_frames) >= min_speech_ratio
