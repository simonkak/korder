"""Shared MPRIS (Media Player Remote Interfacing Specification) helpers.

The protocol every modern Linux player exposes (Spotify, mpv, Firefox
via the plasma-browser-integration bridge, Chromium, VLC, …) over
session D-Bus. We use it for:

  - now_playing action: which player is active, what's the title
  - TTS pause/resume: pause the playing player(s) for the duration
    of a synthesized utterance so it isn't drowned out by music

Originally lived inside `actions/now_playing.py`; extracted so the
TTS path doesn't have to import an action module just to read player
state.

Wire is qdbus6 (Qt 6's D-Bus CLI, ships with Plasma 6).
"""
from __future__ import annotations
import logging
import subprocess
from contextlib import contextmanager

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


@contextmanager
def paused_for_tts():
    """Context manager: pause every Playing MPRIS service on entry,
    resume only those services on exit. No-op when nothing is playing
    or qdbus is missing.

    Used by the TTS path so a synthesized voice doesn't fight music
    underneath. The pause is the same verb a phone-call interruption
    would issue, so well-behaved players (Spotify, mpv, Firefox MPRIS
    bridge) handle it cleanly with state restoration.

    Services that were Paused before the with-block aren't touched —
    we don't want to surprise the user by starting playback they
    didn't ask for.
    """
    paused: list[str] = []
    for service in list_players():
        if player_status(service) == "Playing":
            if qdbus(service, MPRIS_OBJECT, f"{PLAYER_IFACE}.Pause") is not None:
                paused.append(service)
                log.debug("mpris: paused %s for TTS", service)
    try:
        yield paused
    finally:
        for service in paused:
            qdbus(service, MPRIS_OBJECT, f"{PLAYER_IFACE}.Play")
            log.debug("mpris: resumed %s after TTS", service)
