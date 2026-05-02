"""Shared MPRIS (Media Player Remote Interfacing Specification) helpers.

The protocol every modern Linux player exposes (Spotify, mpv, Firefox
via the plasma-browser-integration bridge, Chromium, VLC, …) over
session D-Bus. We use it for:

  - now_playing action: which player is active, what's the title
  - TTS pause/resume in MainWindow: pause the playing player(s) for
    the duration of a synthesized utterance so it isn't drowned out
    by music. The TTS path uses two methods spanning an async call
    (one to pause on speak start, one bound to playback_finished
    to resume) rather than a context manager — see MainWindow's
    _mpris_pause_for_tts / _resume_after_tts.

Originally lived inside `actions/now_playing.py`; extracted so the
TTS path doesn't have to import an action module just to read player
state.

Wire is qdbus6 (Qt 6's D-Bus CLI, ships with Plasma 6).
"""
from __future__ import annotations
import logging
import subprocess

log = logging.getLogger(__name__)

QDBUS_TIMEOUT_S = 2.0
MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_OBJECT = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"


def qdbus(*args: str) -> str | None:
    """Run qdbus6 and return stdout, or None on any failure (missing
    binary, service gone, timeout, non-zero exit). Public so callers
    that need finer control (e.g. now_playing's metadata fetch) can
    use the same wire."""
    try:
        result = subprocess.run(
            ["qdbus6", *args],
            capture_output=True,
            text=True,
            timeout=QDBUS_TIMEOUT_S,
            check=True,
        )
        return result.stdout
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None


def list_players() -> list[str]:
    """Return service names like ['org.mpris.MediaPlayer2.spotify', …]."""
    out = qdbus()
    if out is None:
        return []
    return [
        line.strip()
        for line in out.splitlines()
        if line.strip().startswith(MPRIS_PREFIX)
    ]


def player_status(service: str) -> str:
    """Returns 'Playing' / 'Paused' / 'Stopped' / '' (unknown)."""
    out = qdbus(service, MPRIS_OBJECT, f"{PLAYER_IFACE}.PlaybackStatus")
    return (out or "").strip()


def pick_active_player(services: list[str]) -> str | None:
    """Pick the most likely 'what the user means' player.
    Playing > Paused > anything that's at least responsive."""
    if not services:
        return None
    statuses: dict[str, str] = {s: player_status(s) for s in services}
    for s in services:
        if statuses.get(s) == "Playing":
            return s
    for s in services:
        if statuses.get(s) == "Paused":
            return s
    # Last resort: any player at all, even if its status query failed —
    # we might still get useful metadata out of it.
    return services[0]


def any_playing() -> bool:
    """Fast 'is something audibly playing right now?' check. Used by
    the TTS suppression heuristic to avoid talking over music."""
    for s in list_players():
        if player_status(s) == "Playing":
            return True
    return False
