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
        # Off by default: on the headless intent benchmark thinking adds
        # ~1.3s of latency on E4B and didn't improve pass rate; on E2B it
        # collapses pass rate to 48%. Kept as an opt-in for phrasings the
        # default path can't disambiguate.
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
        # How long ollama keeps the model resident in VRAM after a call.
        # Default 300s matches ollama's own default and keeps follow-up
        # calls warm (~0.2s each). Lower values free VRAM (~9.5 GB on
        # E4B) sooner at the cost of a ~3s cold-load on the next call.
        # 0 unloads immediately. Useful if you want VRAM back for other
        # apps between dictation bursts.
        "keep_alive_s": "300",
    },
    # Wake-word activation. When enabled, an always-on listener runs
    # alongside the regular hotkey path: speaking the configured phrase
    # opens a dictation session as if the hotkey had fired. Off by
    # default — install with `uv sync --extra wake` first.
    "wake": {
        "enabled": "false",
        # Backend that detects the wake phrase. 'openwakeword' is the
        # only option today.
        "engine": "openwakeword",
        # With openwakeword, this is the model name from its pretrained
        # catalog (hey_jarvis, alexa, hey_mycroft, hey_rhasspy). Custom
        # phrases require training your own model.
        "phrase": "hey_jarvis",
        # Detection threshold (0.0-1.0). Lower fires more often (more
        # false positives), higher misses softer wakes.
        "sensitivity": "0.5",
        # If wake fires but no follow-up speech arrives within this
        # many seconds, return to wake-listening. Prevents accidental
        # wakes from leaving the OSD stuck.
        "idle_timeout_s": "5",
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
    # click_by_label action — voice-controlled clicking via the AT-SPI
    # accessibility tree, with an optional Gemma vision fallback when no
    # accessible widget matches the spoken label. Requires `uv sync
    # --extra a11y`. Both knobs no-op when the dep isn't installed.
    "click": {
        # Engage the vision-grounding fallback when AT-SPI returns no
        # match. Set false to make click_by_label AT-SPI-only — fails
        # cleanly on Electron / Chromium subtrees / canvas UIs instead
        # of paying the ~2-3s vision-call latency.
        "vision_fallback": "true",
        # Gemma image-token budget for the vision fallback call. One of
        # 70, 140, 280, 560, 1120 (per Google's vision docs). Higher =
        # better localization on small UI elements at the cost of
        # latency. 560 is a balanced default.
        "vision_token_budget": "560",
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
