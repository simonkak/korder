"""now_playing — voice-queryable "what's currently playing".

Reads track metadata via D-Bus from any active MPRIS player (Spotify,
Firefox, mpv, browser MPRIS-bridge, …) and surfaces it as a desktop
notification via notify-send. With TTS enabled, also speaks the same
text — see speakable_response below.

MPRIS plumbing (player enumeration, status, pause/resume) lives in
korder.audio._mpris and is shared with the TTS suppression and
pause-during-speak paths.
"""
from __future__ import annotations
import logging
import re
import subprocess

from korder.actions.base import Action, register
from korder.audio import _mpris
from korder.ui.progress import emit_progress_speak

log = logging.getLogger(__name__)

_NOTIFY_TIMEOUT_S = 3.0


def _player_metadata(service: str) -> dict[str, str]:
    """Returns {'title', 'artist', 'album'} where present. Each value is
    the empty string if MPRIS didn't report that field."""
    out = _mpris.qdbus(service, _mpris.MPRIS_OBJECT, f"{_mpris.PLAYER_IFACE}.Metadata")
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
    tail = service[len(_mpris.MPRIS_PREFIX):]
    head = tail.split(".", 1)[0]
    pretty = {
        "spotify": "Spotify",
        "firefox": "Firefox",
        "chromium": "Chromium",
        "vlc": "VLC",
        "mpv": "mpv",
        "plasma-browser-integration": "Browser",
    }
    return pretty.get(head, head.replace("-", " ").title())


def _detect_lang(text: str) -> str:
    """Two-state heuristic: 'pl' if Polish-only diacritics are present,
    else 'en'. Good enough for track titles + artist names — gets
    'Małomiasteczkowy' right without misclassifying 'Stressed Out'."""
    if any(ch in text for ch in "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ"):
        return "pl"
    return "en"


def _compose_now_playing() -> tuple[str, str, str] | None:
    """Resolve the currently active player + metadata into a
    (title-line, body-line, lang) triple. Returns None when no MPRIS
    player is available — caller decides what to show in that case.
    Used by both the desktop notification and the TTS speakable.
    """
    services = _mpris.list_players()
    player = _mpris.pick_active_player(services)
    if player is None:
        return None
    md = _player_metadata(player)
    title = md.get("title", "")
    artist = md.get("artist", "")
    if not title and not artist:
        return None
    body = f"{title} — {artist}" if title and artist else (title or artist)
    status = _mpris.player_status(player)
    prefix = "▶  " if status == "Playing" else ("⏸  " if status == "Paused" else "")
    headline = f"{prefix}{_short_player_name(player)}"
    return headline, body, _detect_lang(body)


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


def _spoken_form(body: str, lang: str) -> str:
    """Convert the en-dash-separated notification body into something
    natural for TTS. 'Stressed Out — Twenty One Pilots' →
    'Stressed Out by Twenty One Pilots' in EN,
    'Stressed Out, Twenty One Pilots' in PL."""
    if " — " not in body:
        return body
    title, _, artist = body.partition(" — ")
    return f"{title} by {artist}" if lang == "en" else f"{title}, {artist}"


def _now_playing() -> None:
    composed = _compose_now_playing()
    if composed is None:
        # Nothing playable. Speak too — eyes-busy users wouldn't see
        # the notification, and "no answer" feels like the action
        # broke. Lang heuristic uses the system locale via i18n.
        from korder.ui.i18n import current_locale
        lang = "pl" if current_locale() == "pl" else "en"
        services = _mpris.list_players()
        if services:
            player = _mpris.pick_active_player(services) or services[0]
            headline = f"{_short_player_name(player)}: nothing playing"
            body = "Player is open but no track metadata is available."
            spoken = (
                "Odtwarzacz jest otwarty, ale nie ma żadnego utworu."
                if lang == "pl" else
                "A media player is open but nothing is playing."
            )
        else:
            headline = "Nothing playing"
            body = "No media player is running."
            spoken = "Nic nie gra." if lang == "pl" else "Nothing is playing."
        _notify(headline, body)
        emit_progress_speak(spoken, lang)
        return
    headline, body, lang = composed
    _notify(headline, body)
    # Also push to the speak bus — MainWindow routes to TTS when
    # [tts] enabled. The OSD line shows the same body verbatim so
    # eyes-on users see exactly what the voice is reading.
    emit_progress_speak(_spoken_form(body, lang), lang)


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
