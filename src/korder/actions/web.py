"""URL-driven actions: web search, YouTube, Wikipedia, Maps.

All four follow the same pattern: voice → query string → URL →
``xdg-open`` (routes to the user's default browser). No API keys, no
tokens, works as long as a browser is set as default for http(s).

Queries are LLM-extracted — these actions are regex-unfriendly because
the trigger words ("search for", "youtube", "wikipedia", "navigate to")
are followed by free-form text that should pass through verbatim.

The web-search engine is configurable via ``[web] search_engine``
(default ``duckduckgo``); YouTube/Wikipedia/Maps each go to their
canonical URL with no engine choice. Wikipedia auto-picks language
from the system locale (en or pl).
"""
from __future__ import annotations

import locale
import subprocess
from urllib.parse import quote

from korder import config
from korder.actions.base import Action, register
from korder.ui.i18n import tf
from korder.ui.progress import emit_progress


# Engine-name → URL template. The {q} placeholder gets replaced with
# the percent-encoded query. Add new engines here; the LLM doesn't need
# to know about the engine choice — it only extracts the query.
_ENGINES: dict[str, str] = {
    "duckduckgo": "https://duckduckgo.com/?q={q}",
    "google":     "https://www.google.com/search?q={q}",
    "bing":       "https://www.bing.com/search?q={q}",
    "startpage":  "https://www.startpage.com/do/search?query={q}",
    "ecosia":     "https://www.ecosia.org/search?q={q}",
}
_DEFAULT_ENGINE = "duckduckgo"


def _resolve_engine() -> str:
    """Return the URL template for the configured engine, falling back
    to DuckDuckGo on any unknown name (typo-tolerant)."""
    cfg = config.load()
    name = (cfg["web"]["search_engine"] or "").strip().lower()
    return _ENGINES.get(name, _ENGINES[_DEFAULT_ENGINE])


def _engine_label() -> str:
    """Human-readable name for the active engine — used in progress
    narration. 'duckduckgo' renders as 'DuckDuckGo' (special case)."""
    cfg = config.load()
    raw = (cfg["web"]["search_engine"] or _DEFAULT_ENGINE).strip().lower()
    return {
        "duckduckgo": "DuckDuckGo",
        "google":     "Google",
        "bing":       "Bing",
        "startpage":  "Startpage",
        "ecosia":     "Ecosia",
    }.get(raw, raw.title() or "DuckDuckGo")


def _xdg_open(url: str, *, narrate_label: str, query: str) -> None:
    """Shared xdg-open helper for all URL actions. Emits localized
    "searching X for query…" before, "opened X" after. Failures narrate
    the error instead of silently dropping."""
    emit_progress(tf("progress_searching", engine=narrate_label, query=query))
    try:
        subprocess.run(
            ["xdg-open", url],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        emit_progress(tf("progress_xdg_failed", error=str(e)))
        print(f"[korder] web action: xdg-open failed: {e}", flush=True)
        return
    emit_progress(tf("progress_opened_search", engine=narrate_label))


def _do_web_search(query: str) -> None:
    """Compose the search URL and hand off to xdg-open."""
    if not query.strip():
        return
    template = _resolve_engine()
    url = template.format(q=quote(query.strip()))
    _xdg_open(url, narrate_label=_engine_label(), query=query.strip())


def _system_lang() -> str:
    """Best-effort 2-letter language code from the system locale.
    Used for picking en.wikipedia.org vs pl.wikipedia.org. Falls back
    to English on any introspection failure."""
    try:
        lang = (locale.getlocale()[0] or "")
        if lang:
            return lang.split("_")[0].lower()
    except Exception:
        pass
    return "en"


def _do_youtube_search(query: str) -> None:
    if not query.strip():
        return
    url = f"https://www.youtube.com/results?search_query={quote(query.strip())}"
    _xdg_open(url, narrate_label="YouTube", query=query.strip())


def _do_wikipedia_search(query: str) -> None:
    """Open Wikipedia's Special:Search for the query, on the language
    matching the system locale (Polish → pl.wikipedia.org, otherwise
    en.wikipedia.org). Special:Search redirects to the article when an
    exact title match exists, search results otherwise — exactly the
    "look this up" behavior."""
    if not query.strip():
        return
    lang = _system_lang() if _system_lang() in ("pl", "en", "de", "fr", "es", "it") else "en"
    url = f"https://{lang}.wikipedia.org/wiki/Special:Search?search={quote(query.strip())}"
    _xdg_open(url, narrate_label=f"Wikipedia ({lang})", query=query.strip())


def _do_maps_search(query: str) -> None:
    """Open Google Maps with the query — the same URL `pin a location`
    sharing produces. Works for places, addresses, business names, and
    'directions to X' queries (Maps auto-routes from current location
    when given a destination-like query)."""
    if not query.strip():
        return
    url = f"https://www.google.com/maps/search/{quote(query.strip())}"
    _xdg_open(url, narrate_label="Maps", query=query.strip())


def _query_op(args: dict, fn) -> tuple | None:
    """Common shape for the query-driven actions: extract args.query,
    return a callable op when present, or None (pending — the next
    utterance fills the query). Each action passes its own ``fn``."""
    if not isinstance(args, dict):
        args = {}
    query = (args.get("query") or "").strip()
    if not query:
        return None
    return ("callable", lambda q=query: fn(q))


def _web_search_op(args: dict) -> tuple | None:
    return _query_op(args, _do_web_search)


def _youtube_search_op(args: dict) -> tuple | None:
    return _query_op(args, _do_youtube_search)


def _wikipedia_search_op(args: dict) -> tuple | None:
    return _query_op(args, _do_wikipedia_search)


def _maps_search_op(args: dict) -> tuple | None:
    return _query_op(args, _do_maps_search)


register(Action(
    name="web_search",
    description=(
        "Open the user's default browser on a generic web-search results "
        "page. Use ONLY when the user explicitly asks to SEARCH the web — "
        "phrasings like 'search the web for X', 'google X', 'wyszukaj w "
        "internecie X'. Do NOT use for general factual questions ('what "
        "is X', 'co to jest X', 'how tall is X') — answer those directly "
        "via the response field instead. Only dispatch when the user "
        "wants the browser opened on results. Extract the actual search "
        "terms into params.query without the trigger word."
    ),
    triggers={
        "en": [
            "search the web for",
            "search for",
            "google",
            "look up",
            "search",
        ],
        "pl": [
            "wyszukaj w internecie",
            "wyszukaj",
            "znajdź w internecie",
            "google",
            "wygoogluj",
            "sprawdź w sieci",
        ],
    },
    op_factory=_web_search_op,
    parameters={
        "query": {
            "type": "string",
            "description": "The search terms — what the user wants to find.",
        },
    },
))


register(Action(
    name="youtube_search",
    description=(
        "Open YouTube search results for a query. Use when the user says "
        "YouTube by name or asks to play / watch / find a video of "
        "something. Extract the actual subject into params.query — "
        "without 'YouTube', 'video', 'watch', or 'play' words. Examples: "
        "'play X on youtube' → query='X'; 'find video of X on youtube' "
        "→ query='X'; 'youtube tutorial for X' → query='X tutorial'."
    ),
    triggers={
        "en": [
            "play on youtube",
            "youtube",
            "watch on youtube",
            "find on youtube",
        ],
        "pl": [
            "puść na youtube",
            "youtube",
            "obejrzyj na youtube",
            "znajdź na youtube",
        ],
    },
    op_factory=_youtube_search_op,
    parameters={
        "query": {
            "type": "string",
            "description": "What to search for on YouTube — video title, topic, channel, etc.",
        },
    },
))


register(Action(
    name="wikipedia_search",
    description=(
        "Open Wikipedia in the user's browser for a topic. Use ONLY when "
        "the user explicitly names Wikipedia or asks to LOOK SOMETHING "
        "UP on it — 'wikipedia X', 'look up X on Wikipedia', 'open the "
        "Wikipedia article for X'. Do NOT use for general 'what is X', "
        "'co to jest X', 'tell me about X', 'kim był X' — those are "
        "conversational and should be answered directly via the response "
        "field. Only dispatch when the user wants the article OPENED in "
        "a browser. Picks the Wikipedia language matching the system "
        "locale. Extract the subject into params.query."
    ),
    triggers={
        "en": [
            "wikipedia",
            "look up on wikipedia",
            "read about",
            "tell me about",
            "what is",
            "who is",
        ],
        "pl": [
            "wikipedia",
            "co to jest",
            "kim jest",
            "kim był",
            "kto to",
            "powiedz mi o",
        ],
    },
    op_factory=_wikipedia_search_op,
    parameters={
        "query": {
            "type": "string",
            "description": "The article subject — person, concept, place, event, etc.",
        },
    },
))


register(Action(
    name="maps_search",
    description=(
        "Open the location, address, or directions in a maps app. Use "
        "for any request to navigate to / find / locate a place — "
        "'directions to X', 'navigate to X', 'where is X', 'show me X "
        "on the map'. Routes through Google Maps via the user's default "
        "browser. Extract the destination/place into params.query."
    ),
    triggers={
        "en": [
            "directions to",
            "navigate to",
            "find on map",
            "show on map",
            "where is",
            "open map for",
        ],
        "pl": [
            "nawiguj do",
            "znajdź na mapie",
            "pokaż na mapie",
            "trasa do",
            "gdzie jest",
            "mapa do",
        ],
    },
    op_factory=_maps_search_op,
    parameters={
        "query": {
            "type": "string",
            "description": "The destination — place name, address, business, landmark, etc.",
        },
    },
))
