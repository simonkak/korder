"""Window-management actions backed by the KWin scripting bridge.

Voice commands enabled (English / Polish examples):
- 'focus Firefox', 'przełącz na Firefoxa'        → focus_window(target)
- 'close this window', 'zamknij okno'            → close_window
- 'minimize', 'zminimalizuj'                     → minimize_window
- 'tile right', 'przyciągnij w prawo'            → tile_window(side='right')
- 'maximize', 'maksymalizuj'                     → tile_window(side='maximize')
- 'next desktop', 'następny pulpit'              → next_desktop
- 'previous desktop', 'poprzedni pulpit'         → previous_desktop
- 'send to next desktop', 'wyślij na następny'   → send_window_to_desktop(dir='next')
- 'send to screen 2', 'wyślij na ekran 2'        → send_window_to_screen(n=2)
- 'show all windows', 'pokaż wszystkie okna'     → show_overview
- 'show desktop', 'pokaż pulpit'                 → show_desktop_action

All ops are fire-and-forget through KWin's D-Bus surface — Korder
doesn't wait for visual completion. If KWin scripting isn't available
(non-Plasma Wayland, qdbus6 missing) the helpers degrade to no-op
and the action fires successfully on Korder's side without the
window actually moving."""
from __future__ import annotations
import logging

from korder import kwin
from korder.actions.base import Action, register
from korder.ui.progress import emit_progress

log = logging.getLogger(__name__)


# ---- focus_window --------------------------------------------------------


def _focus_window_op(args: dict) -> tuple | None:
    target = (args or {}).get("target", "")
    if not isinstance(target, str):
        target = ""
    target = target.strip()
    if not target:
        # No target → nothing to focus. Mark as pending so the
        # main_window flow asks for the missing parameter.
        return None
    return ("callable", lambda t=target: _do_focus(t))


def _do_focus(target: str) -> None:
    if kwin.activate_window_by_name(target):
        emit_progress(f"Focusing {target}")
        log.info("focus_window: activated by name %r", target)
    else:
        emit_progress(f"Couldn't focus {target}")
        log.warning("focus_window: KWin script load failed for %r", target)


register(Action(
    name="focus_window",
    description=(
        "Switch keyboard / window focus to a SPECIFIC application or "
        "window. Use when the user names what they want focused — "
        "'focus Kate', 'focus Firefox', 'switch to Konsole', 'przełącz "
        "na Spotify'. Extract the named app or title fragment into "
        "params.target. Matches against window title and application "
        "class via fuzzy token overlap inside KWin, so partial names "
        "and multi-word fragments both work ('focus Firefox How I "
        "Chose a Linux Distro' picks the matching tab among multiple)."
    ),
    triggers={
        "en": [
            "focus",
            "switch to",
            "go to window",
        ],
        "pl": [
            "przełącz na",
            "skup na",
            "fokus na",
        ],
    },
    op_factory=_focus_window_op,
    parameters={
        "target": {
            "type": "string",
            "required": True,
            "description": (
                "App name or window-title fragment to focus. Examples: "
                "'Firefox', 'Kate', 'Spotify', 'Firefox How I Chose'. "
                "Required — without a target Korder asks the user."
            ),
        },
    },
))


# ---- close_window / minimize_window --------------------------------------


def _close_window() -> None:
    if kwin.close_active_window():
        emit_progress("Closed window")
        log.info("close_window: dispatched")
    else:
        emit_progress("Failed to close window")
        log.warning("close_window: KWin script failed")


def _minimize_window() -> None:
    if kwin.minimize_active_window():
        emit_progress("Minimized")
        log.info("minimize_window: dispatched")
    else:
        emit_progress("Failed to minimize")
        log.warning("minimize_window: KWin script failed")


register(Action(
    name="close_window",
    description=(
        "Close the currently active window. Use for 'close this window', "
        "'close the active window', 'zamknij okno', 'zamknij to okno'. "
        "Targets the focused window — does NOT close a specific app by "
        "name (that would require focus_window first). Distinct from "
        "shutdown / cancel / exit_write_mode."
    ),
    triggers={
        "en": ["close window", "close this window"],
        "pl": ["zamknij okno", "zamknij to okno"],
    },
    op_factory=lambda _args: ("callable", _close_window),
))


register(Action(
    name="minimize_window",
    description=(
        "Minimize the currently active window. Use for 'minimize', "
        "'minimize this window', 'zminimalizuj', 'zminimalizuj okno'. "
        "Targets the focused window only."
    ),
    triggers={
        "en": ["minimize", "minimize window"],
        "pl": ["zminimalizuj", "zminimalizuj okno"],
    },
    op_factory=lambda _args: ("callable", _minimize_window),
))


# ---- tile_window ---------------------------------------------------------


_TILE_SIDES = ("left", "right", "top", "bottom", "maximize")


def _tile_window_op(args: dict) -> tuple | None:
    side = (args or {}).get("side", "")
    if not isinstance(side, str):
        side = ""
    side = side.strip().lower()
    if side not in _TILE_SIDES:
        return None
    return ("callable", lambda s=side: _do_tile(s))


def _do_tile(side: str) -> None:
    if kwin.tile_active_window(side):
        emit_progress(f"Tiled {side}" if side != "maximize" else "Maximized")
        log.info("tile_window: %s dispatched", side)
    else:
        emit_progress("Failed to tile window")
        log.warning("tile_window: KWin script failed for side=%r", side)


register(Action(
    name="tile_window",
    description=(
        "Quick-tile the active window to a screen edge or maximize it. "
        "Use for 'tile left', 'tile to the right half', 'snap to bottom', "
        "'maximize', 'przyciągnij w lewo', 'maksymalizuj'. Extract the "
        "side into params.side: 'left' / 'right' / 'top' / 'bottom' for "
        "quick-tile, 'maximize' for full-screen. Required — without a "
        "side Korder asks the user."
    ),
    triggers={
        "en": ["tile", "snap", "maximize"],
        "pl": ["przyciągnij", "maksymalizuj"],
    },
    op_factory=_tile_window_op,
    parameters={
        "side": {
            "type": "string",
            "enum": list(_TILE_SIDES),
            "required": True,
            "description": (
                "Where to tile: 'left' / 'right' / 'top' / 'bottom' for "
                "quick-tile to that screen edge, or 'maximize' for full-"
                "screen. Required."
            ),
        },
    },
))


# ---- desktop switching ---------------------------------------------------


def _next_desktop() -> None:
    if kwin.next_desktop():
        emit_progress("Next desktop")
        log.info("next_desktop: dispatched")
    else:
        emit_progress("Failed to switch desktop")
        log.warning("next_desktop: KWin call failed")


def _previous_desktop() -> None:
    if kwin.previous_desktop():
        emit_progress("Previous desktop")
        log.info("previous_desktop: dispatched")
    else:
        emit_progress("Failed to switch desktop")
        log.warning("previous_desktop: KWin call failed")


register(Action(
    name="next_desktop",
    description=(
        "Switch to the next virtual desktop. Use for 'next desktop', "
        "'switch to next desktop', 'następny pulpit', 'przejdź do "
        "następnego pulpitu'. Wraps around at the last desktop."
    ),
    triggers={
        "en": ["next desktop", "switch desktop"],
        "pl": ["następny pulpit", "kolejny pulpit"],
    },
    op_factory=lambda _args: ("callable", _next_desktop),
))


register(Action(
    name="previous_desktop",
    description=(
        "Switch to the previous virtual desktop. Use for 'previous "
        "desktop', 'poprzedni pulpit'. Wraps at the first desktop."
    ),
    triggers={
        "en": ["previous desktop"],
        "pl": ["poprzedni pulpit"],
    },
    op_factory=lambda _args: ("callable", _previous_desktop),
))


# ---- send-to-desktop / send-to-screen -----------------------------------


def _send_window_to_next_desktop_op(args: dict) -> tuple:
    direction = (args or {}).get("direction", "next")
    if not isinstance(direction, str):
        direction = "next"
    step = -1 if direction.strip().lower() == "previous" else +1
    return ("callable", lambda s=step: _do_send_to_relative_desktop(s))


def _do_send_to_relative_desktop(step: int) -> None:
    if kwin.send_active_to_next_desktop(direction=step):
        verb = "next" if step > 0 else "previous"
        emit_progress(f"Sent window to {verb} desktop")
        log.info("send_window_to_desktop: step=%d dispatched", step)
    else:
        emit_progress("Failed to move window")
        log.warning("send_window_to_desktop: KWin script failed")


register(Action(
    name="send_window_to_desktop",
    description=(
        "Move the active window to the next or previous virtual desktop, "
        "and follow it there. Use for 'send to next desktop', 'move "
        "this to previous desktop', 'wyślij na następny pulpit'. Set "
        "params.direction to 'next' (default) or 'previous'."
    ),
    triggers={
        "en": ["send to next desktop", "move to next desktop"],
        "pl": ["wyślij na następny pulpit", "przenieś na następny pulpit"],
    },
    op_factory=_send_window_to_next_desktop_op,
    parameters={
        "direction": {
            "type": "string",
            "enum": ["next", "previous"],
            "description": (
                "'next' (default) or 'previous'. Determines which way "
                "the window (and the user's view) moves."
            ),
        },
    },
))


def _send_window_to_screen_op(args: dict) -> tuple | None:
    raw = (args or {}).get("screen", None)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if n < 1:
        return None
    return ("callable", lambda x=n: _do_send_to_screen(x))


def _do_send_to_screen(n: int) -> None:
    if kwin.send_active_to_screen(n):
        emit_progress(f"Sent window to screen {n}")
        log.info("send_window_to_screen: n=%d dispatched", n)
    else:
        emit_progress("Failed to move window")
        log.warning("send_window_to_screen: KWin script failed for n=%d", n)


register(Action(
    name="send_window_to_screen",
    description=(
        "Move the active window to a specific screen / monitor. Use for "
        "'send to screen 2', 'move to monitor 1', 'snap to screen 2', "
        "'wyślij na ekran 2'. Extract the 1-indexed screen number into "
        "params.screen — required."
    ),
    triggers={
        "en": ["send to screen", "move to screen", "send to monitor"],
        "pl": ["wyślij na ekran", "przenieś na ekran"],
    },
    op_factory=_send_window_to_screen_op,
    parameters={
        "screen": {
            "type": "integer",
            "required": True,
            "description": (
                "1-indexed screen number to move the active window to. "
                "Required."
            ),
        },
    },
))


# ---- show_overview / show_desktop ---------------------------------------


def _show_overview() -> None:
    if kwin.toggle_overview():
        emit_progress("Showing overview")
        log.info("show_overview: dispatched")
    else:
        emit_progress("Couldn't show overview")
        log.warning("show_overview: KWin Effects.toggleEffect failed")


def _show_desktop_action() -> None:
    if kwin.show_desktop(True):
        emit_progress("Showing desktop")
        log.info("show_desktop: dispatched")
    else:
        emit_progress("Couldn't show desktop")
        log.warning("show_desktop: KWin call failed")


register(Action(
    name="show_overview",
    description=(
        "Toggle the Plasma Overview effect — shows all windows across "
        "all virtual desktops in a navigable grid (default Meta+W). Use "
        "for 'show all windows', 'overview', 'pokaż wszystkie okna'. "
        "Distinct from show_desktop, which minimizes everything."
    ),
    triggers={
        "en": ["overview", "show all windows"],
        "pl": ["pokaż wszystkie okna", "podgląd okien"],
    },
    op_factory=lambda _args: ("callable", _show_overview),
))


register(Action(
    name="show_desktop",
    description=(
        "Minimize every window so the bare desktop is visible. Use for "
        "'show desktop', 'minimize all windows', 'pokaż pulpit'. Distinct "
        "from show_overview — that one keeps windows visible in a grid "
        "for picking; this one hides them."
    ),
    triggers={
        "en": ["show desktop", "minimize all"],
        "pl": ["pokaż pulpit", "zminimalizuj wszystkie"],
    },
    op_factory=lambda _args: ("callable", _show_desktop_action),
))
