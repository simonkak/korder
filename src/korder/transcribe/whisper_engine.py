from __future__ import annotations
import re
import threading
import numpy as np
from pywhispercpp.model import Model

_BRACKET_TAG = re.compile(r"\s*\[[^\]]*\]\s*")


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

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.size == 0:
            return ""
        if not _has_speech_energy(audio):
            return ""
        with self._lock:
            m = self._ensure_loaded()
            segments = m.transcribe(audio)
        text = " ".join(s.text.strip() for s in segments).strip()
        return _strip_annotations(text)


def _strip_annotations(text: str) -> str:
    """Remove Whisper's bracketed sound-event annotations like [noises], [music]."""
    cleaned = _BRACKET_TAG.sub(" ", text).strip()
    return re.sub(r"\s+", " ", cleaned)


def _has_speech_energy(audio: np.ndarray, threshold: float = 0.005) -> bool:
    if audio.size == 0:
        return False
    rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
    return rms >= threshold
