from __future__ import annotations
import configparser
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "korderrc"

DEFAULTS: dict[str, dict[str, str]] = {
    "audio": {
        "sample_rate": "16000",
        "device": "",
    },
    "whisper": {
        "model": "medium",
        "language": "",
        "initial_prompt": "Transkrypcja po polsku z pełnymi znakami diakrytycznymi: ą ę ć ł ń ó ś ź ż. Polish transcription with proper diacritics.",
        "n_threads": "4",
    },
    "inject": {
        "tool": "ydotool",
        "trailing_space": "true",
    },
    "ui": {
        "stay_on_top": "true",
        "non_focusable": "true",
    },
}


def load() -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    cp.read_dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cp.read(CONFIG_PATH)
    return cp


def save(cp: configparser.ConfigParser) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w") as f:
        cp.write(f)
