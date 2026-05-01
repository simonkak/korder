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
        "llm_model": "gemma4:e4b",
    },
    # LLM intent parser tuning. Both knobs apply only when
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
    # Bluetooth headphone integration. Currently scoped to the Sennheiser
    # PXC 550-II Alexa-button + HFP-mic switching. When enabled and the
    # configured device is connected, korder uses its mic instead of the
    # default ALSA/PA device, and pressing the Alexa button toggles
    # recording.
    "bluetooth": {
        "enabled": "false",
        # MAC of the headphones (find with `bluetoothctl devices`).
        "device_mac": "",
        # AMA RFCOMM channel. Default 19 confirmed for PXC 550-II firmware
        # 1.6 (modalias bluetooth:v0492p600Dd0106). Other firmware versions
        # may differ; the listener falls back to a brute-force scan of
        # 14..30 if the default refuses the connection.
        "ama_channel": "19",
        # PipeWire/WirePlumber HFP profile name. The bare name is mSBC on
        # PipeWire; PulseAudio uses "headset-head-unit-msbc". Override if
        # mSBC isn't available and you want to force narrowband CVSD.
        "hfp_profile": "headset-head-unit",
        # When false, korder uses whatever profile is currently active
        # (good for users who pin to HFP permanently). When true (default),
        # korder switches to HFP at start_recording and restores the
        # previous profile at stop_recording — keeps music quality between
        # recordings.
        "switch_profile_for_recording": "true",
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
