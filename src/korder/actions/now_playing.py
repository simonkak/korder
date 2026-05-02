"""now_playing — voice-queryable "what's currently playing".

Reads track metadata via D-Bus from any active MPRIS player (Spotify,
Firefox, mpv, browser MPRIS-bridge, …) and surfaces it as a desktop
notification via notify-send. Pure D-Bus + notify-send — no API keys, no
OAuth, no extra dependencies beyond what KDE already ships.

Picker: prefers a player currently in PlaybackStatus="Playing"; falls
back to "Paused"; final fallback is the first MPRIS service we see.
This matches how a user thinks about "what's playing" — the music
actually coming out of the speakers, not whatever happens to be loaded
in a long-paused tab.
"""
from __future__ import annotations
import logging
import re
import subprocess

from korder.actions.base import Action, register

log = logging.getLogger(__name__)


_QDBUS_TIMEOUT_S = 2.0
_NOTIFY_TIMEOUT_S = 3.0
_MPRIS_PREFIX = "org.mpris.MediaPlayer2."
_MPRIS_OBJECT = "/org/mpris/MediaPlayer2"
_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"


def _qdbus(*args: str) -> str | None:
    """Run qdbus6 and return stdout, or None on any failure (missing
    binary, service gone, timeout, non-zero exit)."""
    try:
        result = subprocess.run(
            ["qdbus6", *args],
            capture_output=True,
            text=True,
            timeout=_QDBUS_TIMEOUT_S,
            check=True,
        )
        return result.stdout
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None


def _list_mpris_players() -> list[str]:
    """Return service names like ['org.mpris.MediaPlayer2.spotify', …]."""
    out = _qdbus()
    if out is None:
        return []
    return [
        line.strip()
        for line in out.splitlines()
        if line.strip().startswith(_MPRIS_PREFIX)
    ]


def _player_status(service: str) -> str:
    """Returns 'Playing' / 'Paused' / 'Stopped' / '' (unknown)."""
    out = _qdbus(service, _MPRIS_OBJECT, f"{_PLAYER_IFACE}.PlaybackStatus")
    return (out or "").strip()


def _pick_active_player(services: list[str]) -> str | None:
    """Pick the most likely 'what the user means' player. Playing > Paused
    > anything that's at least responsive."""
    if not services:
        return None
    statuses: dict[str, str] = {s: _player_status(s) for s in services}
    for s in services:
        if statuses.get(s) == "Playing":
            return s
    for s in services:
        if statuses.get(s) == "Paused":
            return s
    # Last resort: any player at all, even if its status query failed —
    # we might still get useful metadata out of it.
    return services[0]


def _player_metadata(service: str) -> dict[str, str]:
    """Returns {'title', 'artist', 'album'} where present. Each value is
    the empty string if MPRIS didn't report that field."""
    out = _qdbus(service, _MPRIS_OBJECT, f"{_PLAYER_IFACE}.Metadata")
    if out is None:
        return {}
    md: dict[str, str] = {}
    # qdbus6 prints variant maps as one line per entry, like:
    #   xesam:title: Stressed Out
    #   xesam:artist: Twenty One Pilots
    # Lists (xesam:artist is technically an array of strings) flatten to
    # comma-separated; we keep that as-is — good enough for display.
    for line in out.splitlines():
        m = re.match(r"^([\w:]+):\s*(.*)$", line.strip())
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if key == "xesam:title":
            md["title"] = val
        elif key == "xesam:artist":
            md["artist"] = val
        elif key == "xesam:album":
            md["album"] = val
    return md


def _short_player_name(service: str) -> str:
    """'org.mpris.MediaPlayer2.spotify' → 'Spotify'.
    'org.mpris.MediaPlayer2.firefox.instance_1_1234' → 'Firefox'."""
    tail = service[len(_MPRIS_PREFIX):]
    head = tail.split(".", 1)[0]
    # Special-cased prettifications that look better than .title()
    pretty = {
        "spotify": "Spotify",
        "firefox": "Firefox",
        "chromium": "Chromium",
        "vlc": "VLC",
        "mpv": "mpv",
        "plasma-browser-integration": "Browser",
    }
    return pretty.get(head, head.replace("-", " ").title())


def _notify(title: str, body: str) -> None:
    """Fire-and-forget desktop notification via notify-send. Silently
    drops the message if notify-send isn't installed."""
    try:
        subprocess.run(
            [
                "notify-send",
                "-a", "Korder",
                "-i", "media-playback-start",
                title,
                body,
            ],
            capture_output=True,
            timeout=_NOTIFY_TIMEOUT_S,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        log.error("now_playing: notify-send failed: %s", e)


def _now_playing() -> None:
    services = _list_mpris_players()
    player = _pick_active_player(services)
    if player is None:
        _notify("Nothing playing", "No media player is running.")
        return
    md = _player_metadata(player)
    title = md.get("title", "")
    artist = md.get("artist", "")
    if not title and not artist:
        _notify(
            f"{_short_player_name(player)}: nothing playing",
            "Player is open but no track metadata is available.",
        )
        return
    body = f"{title} — {artist}" if title and artist else (title or artist)
    status = _player_status(player)
    prefix = "▶  " if status == "Playing" else ("⏸  " if status == "Paused" else "")
    _notify(f"{prefix}{_short_player_name(player)}", body)


register(Action(
    name="now_playing",
    description=(
        "Tell the user what song / track is currently playing. Use for any "
        "imperative or question asking what is playing now / what this song "
        "is / what music is on. Reads from any active MPRIS player "
        "(Spotify, browsers, mpv, etc.)."
    ),
    triggers={
        "en": [
            "what's playing",
            "what is playing",
            "what song is this",
            "what is this song",
            "now playing",
        ],
        "pl": [
            "co gra",
            "co teraz gra",
            "co teraz leci",
            "co to za piosenka",
            "co to za utwór",
        ],
    },
    op_factory=lambda _args: ("callable", _now_playing),
))
