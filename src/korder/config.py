from __future__ import annotations
import configparser
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "korderrc"

DEFAULTS: dict[str, dict[str, str]] = {
    "audio": {
        "sample_rate": "16000",
        "device": "",
        "gain": "0.7",
        # Lower the system playback volume while Korder is recording, so
        # speaker bleed doesn't confuse Whisper. Restored on stop and on
        # crash via atexit. Only fires when ducking would actually lower
        # the level (no-op if you're already below the target).
        "duck_during_recording": "true",
        "duck_volume_pct": "30",
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
        "llm_model": "gemma4:e4b",
    },
    # LLM intent parser tuning. All knobs apply only when
    # inject.action_parser = "llm".
    "intent": {
        # Enable Gemma's "thinking" reasoning step before answering.
        # Slower (~1-2s vs ~300-500ms) but resolves ambiguous phrasings
        # without explicit trigger lists.
        "thinking_mode": "false",
        # Show every trigger phrase per action in the LLM prompt. When
        # false (default), only the action name + a semantic description
        # are shown; Gemma reasons about whether an utterance matches.
        # Smaller prompt, more flexible — handles synonyms in any
        # language Gemma understands without hand-coding them.
        "show_triggers_in_prompt": "false",
        # Per-call timeout in seconds. Generous default because thinking
        # mode regularly takes 3-5s and a single slow call shouldn't
        # trip the fallback to regex (which loses parameterized actions
        # like Spotify search). Matches the pending-parameter window in
        # main_window so the timeouts compose coherently.
        "timeout_s": "20",
    },
    "ui": {
        "show_history_on_start": "false",
        # Stop the recording session automatically after a command-style
        # action (media, Spotify, key press, subprocess) executes. Doesn't
        # affect pure dictation or mode toggles (Pisz/Przestań) — those
        # keep the session open. Set to false to require manual toggle.
        "auto_stop_after_action": "true",
    },
    # Spotify Web API credentials. Optional — without these, voice search
    # falls back to xdg-open (opens search UI, user clicks). Get them at
    # https://developer.spotify.com/dashboard (no Premium required).
    "spotify": {
        "client_id": "",
        "client_secret": "",
    },
    # Web-search action — picks the engine that "search for X" / "google X" /
    # "wyszukaj X" voice commands route to. Supported: duckduckgo (default),
    # google, bing, startpage, ecosia. Unknown values fall back to duckduckgo.
    "web": {
        "search_engine": "duckduckgo",
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
