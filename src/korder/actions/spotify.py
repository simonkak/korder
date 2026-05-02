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

`kind` may be 'album', 'track', 'artist', or 'playlist'. When the LLM
omits it (no explicit cue in the utterance), the client searches all
four types in one request and picks the closest name match to the
query — see SpotifyClient.search.
"""
import shlex
import subprocess
import time
from urllib.parse import quote

from korder import config
from korder.actions.base import Action, register
from korder.spotify_client import SpotifyClient
from korder.ui.i18n import t, tf
from korder.ui.progress import emit_progress

# Each progress narration line gets at least this long on screen before the
# action moves on to the next step or finishes — so the user can actually
# read "Found album: Linkin Park" before "Playing Linkin Park" overwrites it.
_PROGRESS_DWELL_S = 0.6


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


def _kind_label(kind: str) -> str:
    """Localize the Spotify result kind for inline narration. Falls back
    to the generic 'result' label when the kind is empty or unknown."""
    return t(f"kind_{kind}") if kind else t("kind_result")


def _spotify_play_query(query: str, kind: str) -> None:
    """Resolve voice query → spotify URI via Web API (if configured), then
    play via D-Bus. Falls back to opening search results if API isn't set
    up or returns nothing.

    Emits progress narration ("Searching for X" / "Found album X" /
    "Playing X") via the OSD progress bus so the user sees what the
    action is doing during the Executing state.
    """
    if not query:
        emit_progress(t("progress_opening_spotify"))
        _open_uri_via_dbus_or_xdg("spotify:")
        return
    client = _get_client()
    if client is not None:
        emit_progress(tf("progress_searching_spotify", query=query))
        result = client.search_full(query, kind=kind)
        if result and result.get("uri"):
            name = result.get("name") or query
            emit_progress(tf(
                "progress_found",
                kind=_kind_label(result.get("kind", "")),
                name=name,
            ))
            time.sleep(_PROGRESS_DWELL_S)
            emit_progress(tf("progress_playing", name=name))
            print(f"[korder] Spotify: playing {result['uri']} (kind={result.get('kind')}) for query {query!r}", flush=True)
            _open_uri_via_dbus_or_xdg(result["uri"])
            return
    # Fallback: open search UI, user clicks
    fallback_uri = f"spotify:search:{quote(query)}"
    emit_progress(tf("progress_no_match", query=query))
    print(f"[korder] Spotify: no API result; opening search {fallback_uri}", flush=True)
    _open_uri_via_dbus_or_xdg(fallback_uri)


_VALID_KINDS = ("album", "track", "artist", "playlist")


def _spotify_search_op(args: dict) -> tuple | None:
    if not isinstance(args, dict):
        args = {}
    query = args.get("query", "").strip()
    raw_kind = (args.get("kind") or "").strip().lower()
    kind: str | None = raw_kind if raw_kind in _VALID_KINDS else None
    if not query:
        return None  # pending — wait for the next commit as query
    return ("callable", lambda q=query, k=kind: _spotify_play_query(q, k))


register(Action(
    name="spotify_search",
    description=(
        "Search Spotify and play the result. Use when the user invokes "
        "Spotify by name and provides a query (album / artist / song / "
        "playlist). The word 'Spotify' can appear at the START, END, or "
        "MIDDLE of the utterance ('Spotify play X', 'play X on Spotify', "
        "'Odtwórz X w Spotify'). Extract the actual subject — the song / "
        "album / artist / playlist name — into params.query. Strip out "
        "'Spotify', the imperative verb (play / zagraj / odtwórz / "
        "puść), and any kind cue word; everything else is the query, "
        "even if it's an English title inside a Polish frame.\n"
        "Set params.kind only when the user gives an explicit cue:\n"
        "  - 'album' / 'płyta' / 'krążek' → kind='album'\n"
        "  - 'track' / 'song' / 'utwór' / 'piosenka' → kind='track'\n"
        "  - 'artist' / 'band' / 'wykonawca' / 'zespół' / 'grupa' → kind='artist'\n"
        "  - 'playlist' / 'playlista' / 'lista odtwarzania' → kind='playlist'\n"
        "Otherwise leave kind unset and Spotify will pick the best match "
        "across all four types based on the query."
    ),
    triggers={
        "en": [
            "play on spotify",
            "spotify search",
            "spotify play",
            "search on spotify",
        ],
        "pl": [
            "zagraj na spotify",
            "spotify wyszukaj",
            "puść na spotify",
            "odtwórz w spotify",
            "odtwórz na spotify",
            "odtwórz spotify",
            "włącz w spotify",
            "włącz na spotify",
            "włącz spotify",
        ],
    },
    op_factory=_spotify_search_op,
    parameters={
        # query is the primary param — pending-action follow-up text fills it
        "query": {
            "type": "string",
            "description": "The song, album, artist, or playlist to search for",
        },
        # kind is optional — when unset, the client searches all four
        # types in one request and picks by name-similarity to the query
        # (artist > album > track > playlist within each match tier).
        "kind": {
            "type": "string",
            "enum": list(_VALID_KINDS),
            "description": (
                "What to play. Set only when the user gives an explicit cue "
                "(e.g. 'album', 'track', 'artist', 'playlist' or their "
                "equivalents in the user's language). Otherwise omit."
            ),
        },
    },
))
