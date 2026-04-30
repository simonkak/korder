"""Spotify integration actions.

Phase 1 (this file): xdg-open with a spotify: search URI. Opens the
Spotify desktop client with search results for the query — user clicks
the first result. No API setup needed but doesn't auto-play.

Phase 2 (TODO): Spotify Web API via spotipy + OAuth for true
voice-driven search-and-play. Requires Spotify Premium and a registered
developer app at developer.spotify.com.

Parameter extraction is LLM-only — the regex parser can't extract
free-form query strings. In LLM mode Gemma extracts the post-trigger
text into params.query and the op_factory feeds it to xdg-open.
"""
from korder.actions.base import Action, register


def _spotify_search_op(args: dict) -> tuple:
    query = args.get("query", "").strip() if isinstance(args, dict) else ""
    if not query:
        # No query → just open Spotify itself
        return ("subprocess", ["xdg-open", "spotify:"])
    # Spotify URI scheme: spotify:search:QUERY (URL-encoded)
    from urllib.parse import quote
    return ("subprocess", ["xdg-open", f"spotify:search:{quote(query)}"])


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
