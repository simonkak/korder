from __future__ import annotations
import threading
import numpy as np
from faster_whisper import WhisperModel


class WhisperEngine:
    """Thin wrapper over faster-whisper. Loads lazily on first transcribe()."""

    def __init__(
        self,
        model: str = "base",
        compute_type: str = "int8",
        language: str | None = None,
    ):
        self.model_name = model
        self.compute_type = compute_type
        self.language = language or None
        self._model: WhisperModel | None = None
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> WhisperModel:
        with self._lock:
            if self._model is None:
                self._model = WhisperModel(
                    self.model_name,
                    device="cpu",
                    compute_type=self.compute_type,
                )
        return self._model

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.size == 0:
            return ""
        m = self._ensure_loaded()
        segments, _info = m.transcribe(
            audio,
            language=self.language,
            vad_filter=False,
            beam_size=1,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()
