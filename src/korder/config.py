from __future__ import annotations
import configparser
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "korderrc"

DEFAULTS: dict[str, dict[str, str]] = {
    "audio": {
        "sample_rate": "16000",
        "device": "",
        "gain": "0.7",
    },
    "whisper": {
        "model": "medium",
        "language": "pl",
        "initial_prompt": "",
        "n_threads": "4",
    },
    "inject": {
        "tool": "ydotool",
        "trailing_space": "true",
        "paste_mode": "auto",
        # Inline-action parser: "regex" (fast, English+Polish via hardcoded
        # phrases) or "llm" (slower, smarter — uses local Gemma via ollama,
        # handles natural variations and arbitrary phrasings).
        "action_parser": "regex",
        "llm_model": "gemma4",
    },
    "ui": {
        "show_history_on_start": "false",
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
