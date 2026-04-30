"""Spotify integration actions.

Two paths, picked based on whether you've configured Spotify Web API
credentials:

1. With [spotify] client_id + client_secret in config:
   Voice → Web API search → spotify:track:URI → D-Bus OpenUri → plays the
   actual top track. No clicks required. Free Spotify account works for
   search; playback restrictions on free desktop may still apply.

2. Without API credentials:
   Voice → spotify:search:QUERY → D-Bus OpenUri (or xdg-open fallback) →
   opens search UI, user clicks the first result. Works without any
   account setup.

Setup for path 1 (one-time):
  https://developer.spotify.com/dashboard → create app → copy keys
  ~/.config/korderrc:
    [spotify]
    client_id = ...
    client_secret = ...

Parameter extraction is LLM-only — the regex parser can't pull
free-form query strings out of a transcript.
"""
import shlex
import subprocess
from urllib.parse import quote

from korder import config
from korder.actions.base import Action, register
from korder.spotify_client import SpotifyClient


# One client instance, lazy-initialized on first use. Caches access tokens
# across calls so we don't refetch every search.
_client: SpotifyClient | None = None
_client_inited = False


def _get_client() -> SpotifyClient | None:
    global _client, _client_inited
    if _client_inited:
        return _client
    _client_inited = True
    cfg = config.load()
    cid = cfg["spotify"]["client_id"].strip()
    secret = cfg["spotify"]["client_secret"].strip()
    if not cid or not secret:
        return None
    _client = SpotifyClient(cid, secret)
    return _client


def _open_uri_via_dbus_or_xdg(uri: str) -> None:
    """OpenUri via Spotify's MPRIS interface, fall back to xdg-open if the
    desktop client isn't running (xdg-open will launch it)."""
    quoted = shlex.quote(uri)
    cmd = (
        f"qdbus6 org.mpris.MediaPlayer2.spotify "
        f"/org/mpris/MediaPlayer2 "
        f"org.mpris.MediaPlayer2.Player.OpenUri {quoted} 2>/dev/null "
        f"|| xdg-open {quoted}"
    )
    subprocess.run(["sh", "-c", cmd], check=False, capture_output=True, timeout=10)


def _spotify_play_query(query: str) -> None:
    """Resolve voice query → track URI via Web API (if configured), then
    play via D-Bus. Falls back to opening search results if API isn't set
    up or returns nothing."""
    if not query:
        _open_uri_via_dbus_or_xdg("spotify:")
        return
    client = _get_client()
    if client is not None:
        track_uri = client.search_track(query)
        if track_uri:
            print(f"[korder] Spotify: playing {track_uri} for query {query!r}", flush=True)
            _open_uri_via_dbus_or_xdg(track_uri)
            return
    # Fallback: open search UI, user clicks
    fallback_uri = f"spotify:search:{quote(query)}"
    print(f"[korder] Spotify: no track URI; opening search {fallback_uri}", flush=True)
    _open_uri_via_dbus_or_xdg(fallback_uri)


def _spotify_search_op(args: dict) -> tuple | None:
    query = args.get("query", "").strip() if isinstance(args, dict) else ""
    if not query:
        # Incomplete — signal "pending; need a query" so MainWindow can
        # take the next text commit as the parameter.
        return None
    return ("callable", lambda q=query: _spotify_play_query(q))


register(Action(
    name="spotify_search",
    description="Search Spotify for a song, album, or artist and play it",
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
