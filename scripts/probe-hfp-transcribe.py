"""Run a recorded HFP-mic WAV through Whisper to gauge transcription quality.

Companion to scripts/probe-hfp-mic.sh. Loads the WAV, normalizes to the
float32 16 kHz mono shape that WhisperEngine expects, and prints the
transcript.

Usage (from project root, with the venv):
    uv run python scripts/probe-hfp-transcribe.py /tmp/hfp-mic-sample.wav
"""
from __future__ import annotations

import sys
import wave

import numpy as np

from korder import config
from korder.transcribe.whisper_engine import WhisperEngine


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    path = argv[1]

    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        n = w.getnframes()
        raw = w.readframes(n)

    print(f"loaded: {path}  sr={sr}Hz  channels={ch}  samplewidth={sw}B  frames={n}")
    if sw != 2:
        print("ERROR: WAV must be 16-bit PCM.", file=sys.stderr)
        return 1

    pcm = np.frombuffer(raw, dtype=np.int16)
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1).astype(np.int16)
    audio = pcm.astype(np.float32) / 32768.0

    if sr != 16000:
        # Naïve linear resample — fine for a sanity check.
        new_len = int(len(audio) * 16000 / sr)
        audio = np.interp(
            np.linspace(0, len(audio), new_len, endpoint=False),
            np.arange(len(audio)),
            audio,
        ).astype(np.float32)
        print(f"resampled to 16000Hz ({new_len} samples)")

    cfg = config.load()
    engine = WhisperEngine(
        model=cfg["whisper"]["model"],
        language=cfg["whisper"]["language"] or None,
        n_threads=int(cfg["whisper"]["n_threads"]),
    )
    print(f"transcribing with model={cfg['whisper']['model']} lang={cfg['whisper']['language']!r}...")
    text = engine.transcribe(audio)
    print()
    print("=== transcript ===")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
