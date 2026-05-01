"""Web search via xdg-open.

Voice → query string → URL with the configured search engine →
``xdg-open`` (which routes to the user's default browser). No API keys,
no tokens, works as long as a browser is set as the default for
http(s).

The query is LLM-extracted — this action is regex-unfriendly because the
trigger words ("search for", "google", "wyszukaj") are followed by free-
form text that should pass through verbatim.

Configurable via ``[web] search_engine`` (default ``duckduckgo``). To
swap engines without restarting korder, edit the config and use the
Settings dialog's reload, or restart.
"""
from __future__ import annotations

import subprocess
from urllib.parse import quote

from korder import config
from korder.actions.base import Action, register
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


def _do_web_search(query: str) -> None:
    """Compose the search URL and hand off to xdg-open."""
    if not query.strip():
        return
    template = _resolve_engine()
    url = template.format(q=quote(query.strip()))
    emit_progress(f"Searching {_engine_label()} for {query.strip()}…")
    try:
        subprocess.run(
            ["xdg-open", url],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        emit_progress(f"xdg-open failed: {e}")
        print(f"[korder] web_search: xdg-open failed: {e}", flush=True)
        return
    emit_progress(f"Opened {_engine_label()} search")


def _web_search_op(args: dict) -> tuple | None:
    if not isinstance(args, dict):
        args = {}
    query = (args.get("query") or "").strip()
    if not query:
        return None  # pending — let the next utterance fill the query
    return ("callable", lambda q=query: _do_web_search(q))


register(Action(
    name="web_search",
    description=(
        "Open the user's default browser with a web search. Use whenever "
        "the user asks to search the web, look something up, or invokes a "
        "search engine by name (Google, DuckDuckGo, etc.) with a query. "
        "Extract the actual search terms — everything after the trigger "
        "word — into params.query. Do NOT include the trigger words "
        "themselves ('search', 'google', 'wyszukaj', 'znajdź') in the "
        "query."
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
