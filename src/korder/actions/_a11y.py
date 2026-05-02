"""Thin AT-SPI wrapper for the click_by_label action.

Why a wrapper module:

- Lazy import. PyGObject is in the optional `a11y` extra; Korder must
  boot without it. Importing this module is always cheap; the GI stack
  loads only when ``ensure_loaded()`` is called from inside the click
  flow.
- Plain-dict surface. Functions return dicts like
  ``{"name", "role", "x", "y", "w", "h", "action_names", "_obj"}``,
  not Atspi.Accessible objects. Tests stub a function and feed plain
  dicts — no need to mock the GObject hierarchy.

Public API:

  - is_available() -> bool
  - enumerate_active_window_clickables() -> list[dict]
  - click_widget(widget_dict) -> bool

The ``_obj`` field carries the underlying Atspi.Accessible reference so
``click_widget`` can call ``do_action`` on it. Tests pass dicts without
``_obj`` and the coordinate-click fallback path runs instead.
"""
from __future__ import annotations
import logging
import os
import shutil
import subprocess
from typing import Any

log = logging.getLogger(__name__)

# Roles whose children are worth descending into. Anything not in this
# set is treated as a leaf — interactive (collected) or noise (dropped).
# Keep this conservative; over-descending into a 50k-node browser tree
# is a worse failure than missing a deeply-nested toolbar button.
_CONTAINER_ROLES = frozenset({
    "frame", "window", "dialog", "filler", "panel",
    "tool bar", "menu bar", "menu", "popup menu",
    "page tab list", "scroll pane", "viewport", "split pane",
    "internal frame", "status bar",
    "table", "tree", "tree table", "list",
    "application", "layered pane", "redundant object",
    "section", "form", "grouping", "group box",
    "tool tip",
})

# Roles considered interactive — these are what we collect as match
# candidates. Spoken-label fuzzy match runs against these.
_CLICKABLE_ROLES = frozenset({
    "push button", "menu item", "check menu item", "radio menu item",
    "toggle button", "radio button", "check box",
    "link", "page tab", "tab",
    "list item", "tree item",
    "icon",  # toolbar icons
})

# State names that must be set for a widget to be considered for click.
# Pulled by name (string) rather than enum int so the test fixture
# doesn't need to mirror the Atspi.StateType enum.
_REQUIRED_STATES = ("visible", "showing")
# State that's preferred but not strict — disabled buttons get skipped
# only when this is False.
_PREFERRED_SENSITIVE = "sensitive"

# Cap the BFS by node count, not depth — KDE apps with deep panels +
# Firefox's tree both need to work without a fixed depth cap.
_NODE_CAP = 2000

# Module-level cache. Set by ensure_loaded(). When None, a load hasn't
# been attempted yet; when False, the load failed (extra not
# installed). When a module ref, gi+Atspi imported successfully.
_atspi_module: Any = None
_load_attempted = False


def _resolve_a11y_bus_address() -> str | None:
    """Ask the session D-Bus for the current AT-SPI bus address.

    GLib's AT-SPI binding has its own discovery logic that's flaky on
    Wayland/KDE 6 — it sometimes caches a stale per-session socket
    path (e.g., /run/user/1000/at-spi2-XXXXXX/socket from a previous
    login) instead of the canonical /run/user/.../at-spi/bus_0. The
    org.a11y.Bus.GetAddress D-Bus method always returns the live
    address. We set AT_SPI_BUS_ADDRESS to that value before importing
    Atspi so libatspi's lookup uses our override instead of its
    busted internal cache.
    """
    try:
        result = subprocess.run(
            [
                "dbus-send", "--session", "--print-reply",
                "--dest=org.a11y.Bus", "/org/a11y/bus",
                "org.a11y.Bus.GetAddress",
            ],
            capture_output=True, timeout=2.0, text=True, check=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        log.warning("a11y: D-Bus address query failed: %s", e)
        return None
    # Output format: 'method return ... \n   string "unix:path=/run/.../bus_0"'
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("string "):
            quoted = line[len("string "):].strip()
            if quoted.startswith('"') and quoted.endswith('"'):
                return quoted[1:-1]
    log.warning("a11y: D-Bus address query returned unparseable output: %r", result.stdout)
    return None


def ensure_loaded() -> bool:
    """Attempt to import gi+Atspi. Cached. Returns True iff a usable
    Atspi module is available.

    Tests inject a fake Atspi by setting ``_a11y._atspi_module`` and
    ``_a11y._load_attempted = True`` directly — this function then
    short-circuits to the cached value.
    """
    global _atspi_module, _load_attempted
    if _load_attempted:
        return _atspi_module is not None
    _load_attempted = True
    # Force libatspi to use the D-Bus-resolved bus address. Must
    # happen BEFORE the Atspi import — libatspi reads the env var
    # during its own module-init.
    addr = _resolve_a11y_bus_address()
    prev = os.environ.get("AT_SPI_BUS_ADDRESS")
    log.info("a11y: bus address — resolved=%r prev_env=%r", addr, prev)
    if addr:
        os.environ["AT_SPI_BUS_ADDRESS"] = addr
    try:
        import gi  # type: ignore
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi  # type: ignore
        _atspi_module = Atspi
        return True
    except (ImportError, ValueError) as e:
        log.info("a11y stack unavailable: %s — install with `uv sync --extra a11y`", e)
        _atspi_module = None
        return False


def is_available() -> bool:
    return ensure_loaded()


def _state_set_to_names(accessible) -> set[str]:
    """Convert a state set to a set of lowercase state-name strings."""
    out: set[str] = set()
    try:
        ss = accessible.get_state_set()
    except Exception:
        return out
    try:
        states = ss.get_states()
    except Exception:
        return out
    Atspi = _atspi_module
    if Atspi is None:
        return out
    for s in states:
        try:
            name = Atspi.state_name(s)
        except Exception:
            continue
        if isinstance(name, str):
            out.add(name.lower())
    return out


def _role_name(accessible) -> str:
    try:
        return (accessible.get_role_name() or "").lower()
    except Exception:
        return ""


def _safe_name(accessible) -> str:
    try:
        return accessible.get_name() or ""
    except Exception:
        return ""


def _action_names(accessible) -> list[str]:
    """Return lowercased action names for an accessible's Action interface,
    empty list if none. Tries the GI-style ``get_action_iface()``-derived
    methods first; many GI bindings expose ``get_n_actions`` /
    ``get_action_name`` directly on the Accessible."""
    out: list[str] = []
    try:
        n = accessible.get_n_actions()
    except Exception:
        return out
    for i in range(n):
        try:
            nm = accessible.get_action_name(i)
        except Exception:
            continue
        if isinstance(nm, str):
            out.append(nm.lower())
    return out


def _extents(accessible) -> tuple[int, int, int, int] | None:
    """Return (x, y, w, h) in screen coords, or None on failure.
    Coordinates are screen-space logical pixels; HiDPI scaling is
    handled by the caller when invoking ydotool."""
    Atspi = _atspi_module
    if Atspi is None:
        return None
    try:
        rect = accessible.get_extents(Atspi.CoordType.SCREEN)
    except Exception:
        return None
    try:
        return int(rect.x), int(rect.y), int(rect.width), int(rect.height)
    except Exception:
        return None


def _find_active_window(desktop) -> Any | None:
    """Locate the currently focused top-level window. Walk apps →
    children, return the first one whose state set contains 'active'.
    Returns None if no app reports an active window (mid-switch, no
    foreground)."""
    try:
        n_apps = desktop.get_child_count()
    except Exception:
        return None
    for i in range(n_apps):
        try:
            app = desktop.get_child_at_index(i)
        except Exception:
            continue
        if app is None:
            continue
        try:
            n_wins = app.get_child_count()
        except Exception:
            continue
        for j in range(n_wins):
            try:
                win = app.get_child_at_index(j)
            except Exception:
                continue
            if win is None:
                continue
            states = _state_set_to_names(win)
            if "active" in states:
                return win
    return None


def _walk(window) -> list[dict]:
    """BFS the active window's accessibility tree, returning a list of
    plain-dict widget records for every interactive descendant. Capped
    at _NODE_CAP nodes total to bound walk time on browser-class trees."""
    out: list[dict] = []
    if window is None:
        return out
    queue = [window]
    visited = 0
    while queue and visited < _NODE_CAP:
        node = queue.pop(0)
        visited += 1
        role = _role_name(node)
        states = _state_set_to_names(node)
        is_clickable = role in _CLICKABLE_ROLES
        is_visible = all(s in states for s in _REQUIRED_STATES)
        if is_clickable and is_visible and _PREFERRED_SENSITIVE in states:
            extents = _extents(node)
            if extents is not None:
                x, y, w, h = extents
                # Skip degenerate rectangles — off-screen or zero-area.
                if w > 0 and h > 0:
                    out.append({
                        "name": _safe_name(node),
                        "role": role,
                        "x": x, "y": y, "w": w, "h": h,
                        "action_names": _action_names(node),
                        "_obj": node,
                    })
        if role in _CONTAINER_ROLES or role == "" or not is_clickable:
            try:
                n = node.get_child_count()
            except Exception:
                n = 0
            for k in range(n):
                try:
                    child = node.get_child_at_index(k)
                except Exception:
                    continue
                if child is not None:
                    queue.append(child)
    if visited >= _NODE_CAP:
        log.debug("a11y walk hit node cap (%d) — some elements may be missed", _NODE_CAP)
    return out


def _walk_all_apps(desktop) -> list[dict]:
    """Fallback when no window claims STATE_ACTIVE: walk every app's
    descendant tree and merge the clickables. KDE's a11y bridge often
    doesn't propagate focus state, so STATE_ACTIVE absence is common
    on otherwise-functional trees. Cross-app label collisions are
    rare for spoken UI labels (Polish 'Wyślij', 'Dokumenty', etc.) so
    the fuzzy matcher's quality is preserved."""
    out: list[dict] = []
    try:
        n_apps = desktop.get_child_count()
    except Exception:
        return out
    for i in range(n_apps):
        try:
            app = desktop.get_child_at_index(i)
        except Exception:
            continue
        if app is None:
            continue
        app_name = ""
        try:
            app_name = app.get_name() or ""
        except Exception:
            pass
        # Walk each top-level window inside the app rather than the
        # app itself — the app accessible's role often ISN'T in our
        # _CONTAINER_ROLES set so _walk would short-circuit at it.
        try:
            n_wins = app.get_child_count()
        except Exception:
            continue
        for j in range(n_wins):
            try:
                win = app.get_child_at_index(j)
            except Exception:
                continue
            if win is None:
                continue
            widgets = _walk(win)
            if widgets:
                # Tag each widget with its app name so caller can
                # disambiguate / log per-app counts.
                for w in widgets:
                    w["_app"] = app_name
                out.extend(widgets)
    return out


def enumerate_active_window_clickables() -> list[dict]:
    """Public entry. Lazy-loads Atspi, finds the active window, walks
    the tree. Falls back to walking all registered apps when no
    window has STATE_ACTIVE (common on KDE — a11y bridge focus state
    doesn't always propagate from KWin). Returns [] only when the dep
    is unavailable or the bus is empty."""
    if not ensure_loaded():
        return []
    Atspi = _atspi_module
    try:
        desktop = Atspi.get_desktop(0)
    except Exception as e:
        log.warning("a11y: get_desktop failed: %s", e)
        return []
    window = _find_active_window(desktop)
    if window is not None:
        try:
            win_name = window.get_name() or ""
            app = window.get_application()
            app_name = app.get_name() if app else ""
        except Exception:
            win_name, app_name = "", ""
        widgets = _walk(window)
        log.info(
            "a11y: walked active window app=%r title=%r — %d clickables",
            app_name, win_name, len(widgets),
        )
        return widgets
    # No active window — fall back to a full sweep.
    log.info("a11y: no active window flagged; falling back to all-apps sweep")
    widgets = _walk_all_apps(desktop)
    apps_seen = sorted({w.get("_app", "") for w in widgets})
    log.info("a11y: all-apps sweep — %d clickables across %r", len(widgets), apps_seen)
    return widgets


def _try_named_action(obj, preferred_names: tuple[str, ...] = ("click", "press", "activate", "jump")) -> bool:
    """Attempt to fire a named click-equivalent action on the underlying
    Accessible. Returns True iff one of the preferred actions was
    invoked successfully."""
    if obj is None:
        return False
    try:
        n = obj.get_n_actions()
    except Exception:
        return False
    for i in range(n):
        try:
            name = (obj.get_action_name(i) or "").lower()
        except Exception:
            continue
        if name in preferred_names:
            try:
                obj.do_action(i)
                log.debug("a11y: fired action %r (index %d)", name, i)
                return True
            except Exception as e:
                log.warning("a11y: do_action(%d, %r) failed: %s", i, name, e)
                return False
    return False


def _coordinate_click(x: int, y: int, w: int, h: int) -> bool:
    """Move mouse to widget center and left-click via ydotool. Returns
    True on apparent success. Fails closed if ydotool is missing — the
    AT-SPI named-action path is the primary; this is the fallback for
    widgets that don't expose a click action."""
    if shutil.which("ydotool") is None:
        log.error("a11y: ydotool missing, cannot coordinate-click")
        return False
    cx, cy = x + w // 2, y + h // 2
    log.info("a11y: coordinate-click at (%d,%d) — widget (x=%d y=%d w=%d h=%d)", cx, cy, x, y, w, h)
    try:
        subprocess.run(
            ["ydotool", "mousemove", "--absolute", "-x", str(cx), "-y", str(cy)],
            check=True, capture_output=True, timeout=2.0,
        )
        subprocess.run(
            ["ydotool", "click", "0xC0"],
            check=True, capture_output=True, timeout=2.0,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.error("a11y: coordinate click at (%d,%d) failed: %s", cx, cy, e)
        return False
    return True


def click_widget(widget: dict) -> bool:
    """Click the widget. Tries the AT-SPI named-action path first; falls
    back to a ydotool mouse-click at the widget's center.

    Returns True iff some click was issued. The caller is responsible
    for narration; this function only logs internally."""
    obj = widget.get("_obj")
    if _try_named_action(obj):
        return True
    x = int(widget.get("x", 0))
    y = int(widget.get("y", 0))
    w = int(widget.get("w", 0))
    h = int(widget.get("h", 0))
    if w <= 0 or h <= 0:
        log.warning("a11y: widget has degenerate extents, cannot coordinate-click: %r", widget)
        return False
    return _coordinate_click(x, y, w, h)
