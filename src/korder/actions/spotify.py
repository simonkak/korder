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


def _spotify_play_query(query: str, kind: str) -> None:
    """Resolve voice query → spotify URI via Web API (if configured), then
    play via D-Bus. Falls back to opening search results if API isn't set
    up or returns nothing."""
    if not query:
        _open_uri_via_dbus_or_xdg("spotify:")
        return
    client = _get_client()
    if client is not None:
        uri = client.search(query, kind=kind)
        if uri:
            print(f"[korder] Spotify: playing {uri} (kind={kind}) for query {query!r}", flush=True)
            _open_uri_via_dbus_or_xdg(uri)
            return
    # Fallback: open search UI, user clicks
    fallback_uri = f"spotify:search:{quote(query)}"
    print(f"[korder] Spotify: no API result; opening search {fallback_uri}", flush=True)
    _open_uri_via_dbus_or_xdg(fallback_uri)


def _spotify_search_op(args: dict) -> tuple | None:
    if not isinstance(args, dict):
        args = {}
    query = args.get("query", "").strip()
    kind = args.get("kind", "album").strip().lower()
    if kind not in ("album", "track"):
        kind = "album"
    if not query:
        return None  # pending — wait for the next commit as query
    return ("callable", lambda q=query, k=kind: _spotify_play_query(q, k))


register(Action(
    name="spotify_search",
    description=(
        "Search Spotify and play the result. Use when the user invokes "
        "Spotify by name and provides a query (album / artist / song). "
        "Extract the search target (everything after the Spotify trigger word) "
        "into params.query — do NOT include the word 'Spotify' itself or the "
        "imperative verb in the query. Set params.kind to 'track' only when "
        "the user explicitly indicates a single song (e.g., says 'track', "
        "'song', 'utwór', 'piosenka'). Otherwise default kind='album' — most "
        "queries by band or album name are about playing the body of work."
    ),
    triggers={
        "en": ["play on spotify", "spotify search", "spotify play"],
        "pl": ["zagraj na spotify", "spotify wyszukaj", "puść na spotify"],
    },
    op_factory=_spotify_search_op,
    parameters={
        # query is the primary param — pending-action follow-up text fills it
        "query": {
            "type": "string",
            "description": "The song, album, or artist to search for",
        },
        # kind defaults to album when unspecified — most voice queries
        # ("Linkin Park", "Meteora", "Pink Floyd Wish You Were Here") are
        # about a body of work; single-track requests should set
        # kind="track" explicitly via "track" / "utwór" / "piosenka"
        "kind": {
            "type": "string",
            "enum": ["album", "track"],
            "default": "album",
            "description": "What to play: 'album' (default) or 'track' (single song)",
        },
    },
))
