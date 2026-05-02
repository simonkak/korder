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
    # Spoken responses (issue #2). Optional — opt in with `enabled = true`
    # AND `uv sync --extra tts`. Off by default: most users don't want
    # their computer talking back. When on, only actions that declare a
    # `speakable_response` (e.g. now_playing) speak; flip
    # speak_action_progress = true to also voice progress narration.
    "tts": {
        "enabled": "false",
        # Backend. 'piper' is the only implemented engine today; the
        # knob is here so future additions (espeak-ng, RHVoice) can
        # slot in without a config rename.
        "engine": "piper",
        # Per-language voice IDs (Piper voice catalogue —
        # https://huggingface.co/rhasspy/piper-voices). Models
        # auto-download on first use; pre-fetch with
        # `python -m piper.download_voices VOICE_ID`.
        "voice_en": "en_US-amy-medium",
        "voice_pl": "pl_PL-darkman-medium",
        # Playback speed multiplier (1.0 = normal). Piper's length-
        # scale is the inverse, applied internally.
        "speed": "1.0",
        # Don't speak when something else is already playing audio
        # (Spotify, mpv, browser MPRIS bridges). Default false: the
        # marquee TTS use case (now_playing → "what's playing") is
        # specifically the situation where music IS playing, so
        # suppressing under that condition would silence the feature
        # whenever it's most useful. With suppress on, TTS stays
        # silent during music; with it off (default), TTS pauses
        # the active player, speaks, then resumes it — clean but
        # noticeable dropout window.
        "suppress_when_playing": "false",
        # When true, every progress narration emitted via
        # emit_progress_speak() is voiced too. When false (default),
        # only actions that declare speakable_response speak — keeps
        # TTS scoped to query-style answers (now_playing) instead of
        # chatty "Searching Spotify…" lines.
        "speak_action_progress": "false",
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
