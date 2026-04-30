"""Spotify integration actions.

Uses Spotify's MPRIS D-Bus interface (org.mpris.MediaPlayer2.spotify) for
search-URI navigation when the desktop client is running, with an
xdg-open fallback when it isn't (xdg-open launches Spotify and forwards
the URI). The shell-`||` chain in the subprocess op handles both cases.

Limitation: Spotify's OpenUri handles spotify:track:ID by playing the
track, but spotify:search:QUERY just opens the search view — user
still clicks the first result. For true voice→play, we'd need Phase 2:
Spotify Web API to search→track-ID→OpenUri the track. That requires
Premium + a registered dev app at developer.spotify.com + OAuth.
"""
import shlex
from urllib.parse import quote

from korder.actions.base import Action, register


def _spotify_search_op(args: dict) -> tuple:
    query = args.get("query", "").strip() if isinstance(args, dict) else ""
    if not query:
        return ("subprocess", ["xdg-open", "spotify:"])

    # spotify: URI with the query URL-encoded so things like 'Pink Floyd'
    # don't break the D-Bus argument parsing.
    uri = f"spotify:search:{quote(query)}"
    quoted_uri = shlex.quote(uri)

    # Try D-Bus first (no URL-handler popup, doesn't focus-steal). Fall
    # back to xdg-open if Spotify isn't running so the daemon launches.
    cmd = (
        f"qdbus6 org.mpris.MediaPlayer2.spotify "
        f"/org/mpris/MediaPlayer2 "
        f"org.mpris.MediaPlayer2.Player.OpenUri {quoted_uri} 2>/dev/null "
        f"|| xdg-open {quoted_uri}"
    )
    return ("subprocess", ["sh", "-c", cmd])


register(Action(
    name="spotify_search",
    description="Open Spotify desktop with a search for a song, album, or artist",
    triggers={
        "en": ["play on spotify", "spotify search", "spotify play"],
        "pl": ["zagraj na spotify", "spotify wyszukaj", "puść na spotify"],
    },
    op_factory=_spotify_search_op,
    parameters={
        "query": {
            "type": "string",
            "description": "The song, album, or artist to search for",
        },
    },
))
