from __future__ import annotations
import numpy as np
import webrtcvad


class SpeechDetector:
    """WebRTC VAD wrapper. Classifies 30 ms frames as speech vs non-speech."""

    FRAME_MS = 30
    VALID_RATES = (8000, 16000, 32000, 48000)

    def __init__(self, sample_rate: int = 16000, aggressiveness: int = 2):
        if sample_rate not in self.VALID_RATES:
            raise ValueError(f"webrtcvad requires sample_rate in {self.VALID_RATES}")
        self._vad = webrtcvad.Vad(aggressiveness)
        self.sample_rate = sample_rate
        self.frame_size = sample_rate * self.FRAME_MS // 1000

    def find_trailing_silence(self, audio: np.ndarray) -> tuple[int, int]:
        """Walk backwards through 30 ms frames; return (speech_end_sample, trailing_silence_ms)."""
        pcm = self._to_pcm16(audio)
        n_frames = len(pcm) // self.frame_size
        for i in range(n_frames - 1, -1, -1):
            frame = pcm[i * self.frame_size:(i + 1) * self.frame_size]
            if self._vad.is_speech(frame.tobytes(), self.sample_rate):
                speech_end = (i + 1) * self.frame_size
                silence_samples = audio.size - speech_end
                return speech_end, int(silence_samples * 1000 / self.sample_rate)
        return 0, int(audio.size * 1000 / self.sample_rate)

    def has_speech(self, audio: np.ndarray, min_speech_ms: int = 150) -> bool:
        """True if at least min_speech_ms of contiguous speech is present.
        Contiguous matters: a single keystroke frame won't satisfy this."""
        pcm = self._to_pcm16(audio)
        n_frames = len(pcm) // self.frame_size
        if n_frames == 0:
            return False
        needed = max(1, min_speech_ms // self.FRAME_MS)
        streak = 0
        for i in range(n_frames):
            frame = pcm[i * self.frame_size:(i + 1) * self.frame_size]
            if self._vad.is_speech(frame.tobytes(), self.sample_rate):
                streak += 1
                if streak >= needed:
                    return True
            else:
                streak = 0
        return False

    @staticmethod
    def _to_pcm16(audio: np.ndarray) -> np.ndarray:
        clipped = np.clip(audio, -1.0, 1.0)
        return (clipped * 32767.0).astype(np.int16)
