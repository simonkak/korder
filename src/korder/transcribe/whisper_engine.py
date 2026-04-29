from __future__ import annotations
import threading
import numpy as np
from pywhispercpp.model import Model


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
                }
                if self.language:
                    kwargs["language"] = self.language
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
        with self._lock:
            m = self._ensure_loaded()
            segments = m.transcribe(audio)
        return " ".join(s.text.strip() for s in segments).strip()
