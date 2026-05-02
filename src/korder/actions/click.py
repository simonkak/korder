"""Voice-controlled clicking via the AT-SPI accessibility tree, with
a Gemma 4 vision fallback for surfaces that don't publish a11y info
(Electron without --force-renderer-accessibility, web pages, custom
canvases).

Flow per ``click X``:

  1. AT-SPI: walk active window's a11y tree, fuzzy-match X against
     widget names. Hit → fire the widget's named click action (or
     coordinate-click fallback). Miss → step 2.
  2. Vision: capture screen, ask Gemma to localize the named control,
     parse box_2d, click center. Skipped when [click] vision_fallback
     = false in config, or when ollama is unreachable.
  3. Both miss → narrate "no button matching X", do nothing.

The action stays registered (visible in the LLM prompt) even when the
optional `a11y` extra isn't installed — the on-use narration tells
the user how to enable it. This means voice intent ("click submit")
gets routed correctly by Gemma; the failure mode is "narrate, don't
crash" rather than "the model never knew this action existed".
"""
from __future__ import annotations
import difflib
import logging
import re
import unicodedata

from korder import config
from korder.actions import _a11y, _vision_click
from korder.actions.base import Action, register
from korder.ui.i18n import tf
from korder.ui.progress import emit_progress

log = logging.getLogger(__name__)

# Fuzzy-match cutoff. 0.6 lets "wyslij" match "Wyślij" after diacritic
# fold, and "submit" match "&Submit"/"Submit..." after mnemonic strip,
# without admitting "camcel" → "Cancel" (edit distance too high).
_MATCH_CUTOFF = 0.6


def _strip_label(s: str) -> str:
    """Normalize a widget label or spoken label for fuzzy compare:
    drop & mnemonics, parenthesized accelerators, trailing ellipsis,
    NFKD-fold diacritics, lowercase, collapse whitespace."""
    if not s:
        return ""
    # Drop & mnemonic markers (Qt / GTK use "&File", "_File")
    s = s.replace("&", "").replace("_", " ")
    # Drop "(W)" / "(F)" accelerator suffixes some KDE menus add
    s = re.sub(r"\(\w\)", "", s)
    # Drop trailing "..." / "…" — common on dialog-opening menu items
    s = s.replace("…", "").replace("...", "")
    # NFKD fold diacritics → ASCII (Wyślij → Wyslij), then lowercase
    nfkd = unicodedata.normalize("NFKD", s)
    ascii_s = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(ascii_s.lower().split())


def _find_match(label: str, widgets: list[dict]) -> dict | None:
    """Pick the best-matching clickable for the spoken label.

    Strategy:
      1. Build a normalized-name map.
      2. Exact match wins regardless of cutoff.
      3. difflib.get_close_matches with _MATCH_CUTOFF for fuzzy.
      4. Among multiple fuzzy ties, pick the smallest enclosing
         rectangle — innermost dialog wins over a backdrop control
         with a similar name.

    Returns the widget dict, or None when no match clears the cutoff.
    """
    if not label or not widgets:
        return None
    target = _strip_label(label)
    if not target:
        return None
    normalized = []
    for w in widgets:
        norm = _strip_label(w.get("name") or "")
        if norm:
            normalized.append((norm, w))
    if not normalized:
        return None
    # Exact match short-circuit (case/diacritic insensitive)
    exact = [w for n, w in normalized if n == target]
    if exact:
        return _smallest_widget(exact)
    # Fuzzy match
    candidates = difflib.get_close_matches(
        target,
        [n for n, _ in normalized],
        n=5,
        cutoff=_MATCH_CUTOFF,
    )
    if not candidates:
        return None
    matched = [w for n, w in normalized if n in candidates]
    return _smallest_widget(matched)


def _smallest_widget(widgets: list[dict]) -> dict:
    """Pick the one with smallest area — proxy for 'innermost dialog'.
    Useful when an OK button exists in both a parent window and a
    modal child."""
    return min(widgets, key=lambda w: int(w.get("w", 0)) * int(w.get("h", 0)) or 10**9)


def _vision_fallback_enabled() -> bool:
    cfg = config.load()
    val = (cfg["click"]["vision_fallback"] or "").strip().lower()
    return val in ("true", "1", "yes", "on")


def _llm_model() -> str:
    cfg = config.load()
    return (cfg["inject"]["llm_model"] or "gemma4:e4b").strip()


def _do_click(label: str) -> None:
    """Orchestrator. Runs the AT-SPI path, then vision fallback if
    enabled, narrating each step. Always returns; never raises."""
    label = (label or "").strip()
    if not label:
        log.debug("click: empty label, no-op")
        return

    # AT-SPI tier
    if not _a11y.is_available():
        emit_progress(tf("progress_a11y_missing"))
        log.info("click: a11y unavailable, skipping AT-SPI tier")
        widgets = []
    else:
        widgets = _a11y.enumerate_active_window_clickables()

    match = _find_match(label, widgets) if widgets else None
    if match is not None:
        name = match.get("name") or label
        emit_progress(tf("progress_clicking", name=name))
        log.info(
            "click: AT-SPI match %r for label %r at (x=%s,y=%s,w=%s,h=%s) app=%r actions=%r",
            name, label,
            match.get("x"), match.get("y"), match.get("w"), match.get("h"),
            match.get("_app", ""),
            match.get("action_names"),
        )
        if _a11y.click_widget(match):
            return
        log.warning("click: a11y click_widget failed for %r — falling through to vision", name)

    # Vision fallback tier
    if not _vision_fallback_enabled():
        emit_progress(tf("progress_no_match_button", label=label))
        log.info("click: no AT-SPI match for %r, vision fallback disabled", label)
        return
    emit_progress(tf("progress_clicking", name=label))
    log.info("click: invoking vision fallback for %r", label)
    coords = _vision_click.find_click_target(label, _llm_model())
    if coords is None:
        emit_progress(tf("progress_no_match_button", label=label))
        log.info("click: vision fallback returned no match for %r", label)
        return
    cx, cy = coords
    if not _vision_click.click_at(cx, cy):
        emit_progress(tf("progress_no_match_button", label=label))
        return
    log.info("click: vision-fallback clicked at (%d,%d) for %r", cx, cy, label)


def _click_op_factory(args: dict) -> tuple | None:
    """Returns ('callable', closure) when label is present, else None.
    Returning None signals the pending-parameter flow — the user said
    'click' alone, the LLM didn't supply a label, so the regex parser
    or LLM dispatcher emits ('pending_action', 'click_by_label') and
    the next utterance becomes the label. Same shape as web_search."""
    if not isinstance(args, dict):
        return None
    label = (args.get("label") or "").strip()
    if not label:
        return None
    return ("callable", lambda l=label: _do_click(l))


register(Action(
    name="click_by_label",
    description=(
        "Click a NAMED on-screen button, link, menu item, tab, or "
        "control in the currently focused application. Use ONLY when "
        "the user says 'click X' / 'kliknij X' / 'press the X button' "
        "where X is a label visible on screen. Do NOT use for "
        "press_enter (that's the Enter key, not a named widget). Do "
        "NOT use for web_search. Extract the visible button label "
        "into params.label, stripping the verb ('click' / 'kliknij' / "
        "'press the')."
    ),
    triggers={
        "en": [
            "click on", "click the", "press the button", "tap on", "click",
        ],
        "pl": [
            "kliknij w", "kliknij na", "naciśnij przycisk", "kliknij",
        ],
    },
    op_factory=_click_op_factory,
    parameters={
        "label": {
            "type": "string",
            "description": "The visible button/link/control label, exactly as it appears on screen.",
        },
    },
))
