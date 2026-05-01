"""Tiny locale-aware string lookup for OSD hints. Avoids pulling in Qt's
full translation infrastructure for a handful of phrases.

Two surfaces:

  - ``t(key)`` — direct lookup. Returns the key itself if no translation
    exists in either the user's locale bundle or the English fallback —
    visible breadcrumb that a string was missed.
  - ``tf(key, **kwargs)`` — t() + str.format() in one call. Use for
    templated strings like progress narration: ``tf("progress_playing",
    name="Linkin Park")``.
"""
from __future__ import annotations
from PySide6.QtCore import QLocale


_STRINGS: dict[str, dict[str, str]] = {
    "pl": {
        # OSD placeholders + state labels
        "listening_placeholder": "Powiedz polecenie…",
        "thinking": "myślę…",
        "executing": "wykonuję",
        "transcribing": "transkrybuję…",
        "pending_param_hint": "powiedz parametr…",
        "write_mode_on": "tryb pisania",
        "preview_mode": "tryb podglądu",
        "state_listening": "Słucham",
        "state_thinking": "Myślę",
        "state_executing": "Wykonuję",
        "state_pending": "Czekam",
        "state_committed": "Gotowe",
        "press_to_cancel": "Naciśnij ESC, aby anulować",
        # Pending-action parameter prompts. Per-param keys give richer
        # hints than the generic pending_param_hint when an action
        # declares a known parameter name (query / kind / address / …).
        "say_the_param_query": "powiedz zapytanie…",
        "say_the_param_kind": "powiedz rodzaj…",
        # Spotify "kind" labels — used in progress narration like
        # "Found album: Linkin Park"
        "kind_album": "album",
        "kind_track": "utwór",
        "kind_artist": "wykonawcę",
        "kind_playlist": "playlistę",
        "kind_result": "wynik",
        # Status bar (history window)
        "status_idle": "Bezczynny.",
        "status_listening": "Słucham…",
        "status_transcribing": "Transkrybuję…",
        "status_cancelled": "Anulowano.",
        "status_write_mode_on": "Tryb pisania włączony",
        "status_write_mode_off": "Tryb pisania wyłączony (tylko podgląd)",
        # Action progress narration. Templates interpolated via tf();
        # placeholder names ({engine}, {query}, {name}, {kind}, {error})
        # stay stable across locales.
        "progress_searching": "Szukam w {engine}: {query}…",
        "progress_opened_search": "Otwarto wyszukiwanie {engine}",
        "progress_searching_spotify": "Szukam w Spotify: {query}…",
        "progress_found": "Znaleziono {kind}: {name}",
        "progress_playing": "Odtwarzam: {name}",
        "progress_opening_spotify": "Otwieram Spotify…",
        "progress_no_match": "Brak wyniku — otwieram wyszukiwanie Spotify dla {query}",
        "progress_locking_screen": "Blokuję ekran…",
        "progress_lock_failed": "Blokada nieudana: {error}",
        "progress_xdg_failed": "xdg-open nie powiódł się: {error}",
    },
    "en": {
        "listening_placeholder": "Say a command…",
        "thinking": "thinking…",
        "executing": "executing",
        "transcribing": "transcribing…",
        "pending_param_hint": "say the parameter…",
        "write_mode_on": "write mode",
        "preview_mode": "preview mode",
        "state_listening": "Listening",
        "state_thinking": "Thinking",
        "state_executing": "Executing",
        "state_pending": "Awaiting",
        "state_committed": "Done",
        "press_to_cancel": "Press ESC to cancel",
        "say_the_param_query": "say the query…",
        "say_the_param_kind": "say the kind…",
        "kind_album": "album",
        "kind_track": "track",
        "kind_artist": "artist",
        "kind_playlist": "playlist",
        "kind_result": "result",
        "status_idle": "Idle.",
        "status_listening": "Listening…",
        "status_transcribing": "Transcribing…",
        "status_cancelled": "Cancelled.",
        "status_write_mode_on": "Write mode ON",
        "status_write_mode_off": "Write mode OFF (preview only)",
        "progress_searching": "Searching {engine} for {query}…",
        "progress_opened_search": "Opened {engine} search",
        "progress_searching_spotify": "Searching Spotify for {query}…",
        "progress_found": "Found {kind}: {name}",
        "progress_playing": "Playing {name}",
        "progress_opening_spotify": "Opening Spotify…",
        "progress_no_match": "No match — opening Spotify search for {query}",
        "progress_locking_screen": "Locking screen…",
        "progress_lock_failed": "Lock failed: {error}",
        "progress_xdg_failed": "xdg-open failed: {error}",
    },
}


def _bundle() -> dict[str, str]:
    lang = QLocale.system().name().split("_")[0].lower()
    return _STRINGS.get(lang) or _STRINGS["en"]


def t(key: str) -> str:
    """Return the localized string for the user's system locale, or the
    English fallback if the locale isn't covered. Returns the key itself
    when no translation exists in any bundle."""
    return _bundle().get(key) or _STRINGS["en"].get(key) or key


def tf(key: str, **kwargs: object) -> str:
    """Localize + interpolate. ``t(key).format(**kwargs)`` in one call,
    with graceful fallback to the bare template when the caller forgot
    a placeholder (so a missing kwarg can't crash the OSD render)."""
    template = t(key)
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template
