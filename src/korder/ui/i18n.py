"""Tiny locale-aware string lookup for OSD hints. Avoids pulling in Qt's
full translation infrastructure for a handful of phrases."""
from __future__ import annotations
from PySide6.QtCore import QLocale


_STRINGS: dict[str, dict[str, str]] = {
    "pl": {
        "listening_placeholder": "Powiedz polecenie…",
        "thinking": "myślę…",
        "executing": "wykonuję",
        "pending_param_hint": "powiedz parametr…",
        "write_mode_on": "tryb pisania",
        "preview_mode": "tryb podglądu",
    },
    "en": {
        "listening_placeholder": "Say a command…",
        "thinking": "thinking…",
        "executing": "executing",
        "pending_param_hint": "say the parameter…",
        "write_mode_on": "write mode",
        "preview_mode": "preview mode",
    },
}


def t(key: str) -> str:
    """Return the localized string for the user's system locale, or the
    English fallback if the locale isn't covered."""
    lang = QLocale.system().name().split("_")[0].lower()
    bundle = _STRINGS.get(lang) or _STRINGS["en"]
    return bundle.get(key) or _STRINGS["en"].get(key) or key
