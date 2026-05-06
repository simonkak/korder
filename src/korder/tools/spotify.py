"""Spotify discovery tool.

First parametric tool in the catalogue: takes a query (and optional
kind) and returns the top N Spotify results so the LLM can pick a
specific URI to dispatch to ``spotify_play``. Use case: ambiguous
queries where multiple tracks / artists share a name (the user says
"play Numb" — could be Linkin Park or Usher) and the action's
internal best-guess search gets it wrong.

Reuses the cached SpotifyClient from ``actions.spotify`` so a single
OAuth token survives across both the action's internal search and
this tool's. No second auth, no second config surface."""
from __future__ import annotations
import logging

from korder.actions import spotify as spotify_action
from korder.tools.base import Tool, register_tool

log = logging.getLogger(__name__)

_VALID_KINDS = ("track", "album", "artist", "playlist")


def _search_spotify(*, query: str = "", kind: str = "") -> list[dict]:
    """Return up to 5 top Spotify results matching ``query``. Empty
    list on any failure (no API credentials, network error, empty
    query). The ``kind`` arg restricts to one of track/album/artist/
    playlist; omit (or leave empty) to mix all four."""
    q = (query or "").strip()
    if not q:
        return []
    client = spotify_action._get_client()
    if client is None:
        log.info("search_spotify: no Spotify credentials configured")
        return []
    k = (kind or "").strip().lower()
    if k and k not in _VALID_KINDS:
        k = ""
    try:
        return client.search_top_n(q, kind=k or None, n=5)
    except Exception as e:
        log.warning("search_spotify: failed: %s", e)
        return []


register_tool(Tool(
    name="search_spotify",
    description=(
        "Search Spotify for tracks, albums, artists, or playlists "
        "matching a query. Returns up to 5 top results as "
        "{uri, name, kind, artist?}. Call BEFORE filling spotify_play "
        "params when the user's query is AMBIGUOUS — multiple tracks "
        "share a name, the user gestured at something they listened "
        "to recently, the prior topic only partially disambiguates. "
        "After seeing results, dispatch with spotify_play{uri: ...} "
        "to play your chosen specific URI without a second search. "
        "Skip this tool for clear unambiguous queries (a unique "
        "artist name like 'Linkin Park'); spotify_play's internal "
        "search handles those without an extra round-trip."
    ),
    executor=_search_spotify,
    args_schema={
        "query": {
            "type": "string",
            "description": (
                "Free-form Spotify search query. Pass the user's "
                "literal words for the song/album/artist they want, "
                "stripped of 'Spotify' and the play verb."
            ),
        },
        "kind": {
            "type": "string",
            "enum": list(_VALID_KINDS),
            "description": (
                "Optional: restrict results to one type. Useful when "
                "the user gave an explicit cue ('that ALBUM by X'). "
                "Omit to search all four types."
            ),
        },
    },
))
