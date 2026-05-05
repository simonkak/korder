"""KWin scripting bridge for window-aware voice actions.

Korder's window manipulation goes through three D-Bus surfaces on
KWin:

- /KWin (org.kde.KWin):   currentDesktop, nextDesktop, previousDesktop,
                          setCurrentDesktop, showDesktop. Direct calls,
                          no scripting needed.
- /Effects (org.kde.kwin.Effects):
                          toggleEffect("overview" / "windowview").
                          Direct call.
- /Scripting (org.kde.kwin.Scripting):
                          loadScript / start / unloadScript. Used for
                          per-window operations the JS API exposes
                          (close, minimize, tile, send-to-desktop,
                          send-to-screen, activate-by-name) but the
                          static D-Bus interface doesn't.

The scripting path writes a temp JS file, loads it, starts (which
runs all loaded scripts to completion), and unloads. JS bodies are
generated with json.dumps to embed strings safely. We don't read
data BACK from scripts in this module — every operation is fire-and-
forget. Window resolution by name is done inside the script (token-
overlap match against caption + resourceClass) so the script can
both pick the target AND act on it without a round-trip.

If KWin's qdbus surface is unavailable (different Plasma version,
qdbus6 missing, kwin not running), every helper here returns False
and logs a warning. Voice actions degrade to no-op so a stale
session doesn't get stuck mid-utterance."""
from __future__ import annotations
import json
import logging
import os
import subprocess
import tempfile

log = logging.getLogger(__name__)

_QDBUS_TIMEOUT_S = 2.0
_KWIN_SERVICE = "org.kde.KWin"
_OBJ_KWIN = "/KWin"
_OBJ_EFFECTS = "/Effects"
_OBJ_SCRIPTING = "/Scripting"
_IFACE_KWIN = "org.kde.KWin"
_IFACE_EFFECTS = "org.kde.kwin.Effects"
_IFACE_SCRIPTING = "org.kde.kwin.Scripting"


def _qdbus(*args: str) -> str | None:
    """qdbus6 wrapper with the same fail-soft contract as
    audio/_mpris.qdbus — returns None on any failure (binary missing,
    timeout, non-zero exit) so callers can degrade gracefully."""
    try:
        result = subprocess.run(
            ["qdbus6", *args],
            capture_output=True,
            text=True,
            timeout=_QDBUS_TIMEOUT_S,
            check=True,
        )
        return result.stdout
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        log.warning("kwin: qdbus failed (%s): %s", " ".join(args), e)
        return None


# ---- direct /KWin operations --------------------------------------------


def next_desktop() -> bool:
    return _qdbus(_KWIN_SERVICE, _OBJ_KWIN, f"{_IFACE_KWIN}.nextDesktop") is not None


def previous_desktop() -> bool:
    return _qdbus(_KWIN_SERVICE, _OBJ_KWIN, f"{_IFACE_KWIN}.previousDesktop") is not None


def set_current_desktop(n: int) -> bool:
    """1-indexed (KWin convention)."""
    return _qdbus(
        _KWIN_SERVICE, _OBJ_KWIN, f"{_IFACE_KWIN}.setCurrentDesktop", str(int(n))
    ) is not None


def show_desktop(showing: bool) -> bool:
    """Hide every window and show the bare desktop (or undo). KWin's
    showDesktop is destructive in the sense that it minimizes every
    window — the inverse path (showing=False) is needed to bring them
    back."""
    return _qdbus(
        _KWIN_SERVICE, _OBJ_KWIN, f"{_IFACE_KWIN}.showDesktop",
        "true" if showing else "false",
    ) is not None


# ---- direct /Effects operations -----------------------------------------


def toggle_overview() -> bool:
    """The Plasma Overview effect (default Meta+W). Shows all windows
    across all desktops in a navigable grid."""
    return _qdbus(
        _KWIN_SERVICE, _OBJ_EFFECTS, f"{_IFACE_EFFECTS}.toggleEffect", "overview"
    ) is not None


def toggle_windowview() -> bool:
    """The Present Windows / windowview effect — current desktop only,
    spread out for picking."""
    return _qdbus(
        _KWIN_SERVICE, _OBJ_EFFECTS, f"{_IFACE_EFFECTS}.toggleEffect", "windowview"
    ) is not None


# ---- /Scripting infrastructure ------------------------------------------


def _run_script(js_body: str) -> bool:
    """Write a tempfile, load+start+unload via /Scripting D-Bus.
    Returns True iff the load+start sequence succeeded; doesn't wait
    for the script's body to complete (KWin's Scripting.start is
    fire-and-forget — JS callbacks resolve on KWin's event loop).

    The unloadScript step takes the tempfile path as the plugin name
    (which is what KWin records when loadScript was called without
    an explicit pluginName). Best-effort cleanup — if it fails, the
    only consequence is a defunct script entry in KWin's loaded list,
    which is harmless until Korder shuts down."""
    fd, script_path = tempfile.mkstemp(prefix="korder-kwin-", suffix=".js")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(js_body)
        load = _qdbus(
            _KWIN_SERVICE, _OBJ_SCRIPTING,
            f"{_IFACE_SCRIPTING}.loadScript", script_path,
        )
        if load is None:
            return False
        start = _qdbus(
            _KWIN_SERVICE, _OBJ_SCRIPTING, f"{_IFACE_SCRIPTING}.start"
        )
        # unload returns bool; we don't need the value but we want to
        # detach the script from KWin's loaded list so a long session
        # doesn't accumulate stale scripts.
        _qdbus(
            _KWIN_SERVICE, _OBJ_SCRIPTING,
            f"{_IFACE_SCRIPTING}.unloadScript", script_path,
        )
        return start is not None
    except OSError as e:
        log.warning("kwin: script tempfile error: %s", e)
        return False
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass


# ---- /Scripting per-window operations -----------------------------------


def close_active_window() -> bool:
    return _run_script(
        "if (workspace.activeWindow) { workspace.activeWindow.closeWindow(); }"
    )


def minimize_active_window() -> bool:
    return _run_script(
        "if (workspace.activeWindow) { workspace.activeWindow.minimized = true; }"
    )


_VALID_TILE_SIDES = ("left", "right", "top", "bottom", "maximize")


def tile_active_window(side: str) -> bool:
    """side: 'left' | 'right' | 'top' | 'bottom' for quick-tile to
    that screen edge, or 'maximize' for full-screen tile."""
    if side not in _VALID_TILE_SIDES:
        log.warning("kwin: invalid tile side %r", side)
        return False
    if side == "maximize":
        # KWin's quick-tile-maximize on a tiled window untiles; for
        # 'maximize' we want a hard maximize, not a quick-tile.
        body = (
            "if (workspace.activeWindow) {"
            " workspace.slotWindowMaximize(); }"
        )
    else:
        slot = {
            "left": "slotWindowQuickTileLeft",
            "right": "slotWindowQuickTileRight",
            "top": "slotWindowQuickTileTop",
            "bottom": "slotWindowQuickTileBottom",
        }[side]
        body = f"if (workspace.activeWindow) {{ workspace.{slot}(); }}"
    return _run_script(body)


def send_active_to_desktop(n: int) -> bool:
    """Move the active window to virtual desktop N (1-indexed). KWin 6
    desktops are objects, not ints — so we look up the matching desktop
    in workspace.desktops and assign window.desktops = [target]."""
    js = f"""
    (function() {{
        const w = workspace.activeWindow;
        if (!w) return;
        const idx = {int(n)} - 1;
        const desks = workspace.desktops;
        if (idx < 0 || idx >= desks.length) return;
        w.desktops = [desks[idx]];
    }})();
    """
    return _run_script(js)


def send_active_to_next_desktop(direction: int = +1) -> bool:
    """direction: +1 forward, -1 back. Wraps at the end."""
    js = f"""
    (function() {{
        const w = workspace.activeWindow;
        if (!w) return;
        const desks = workspace.desktops;
        if (desks.length === 0) return;
        const cur = workspace.currentDesktop;
        let idx = desks.indexOf(cur);
        if (idx < 0) idx = 0;
        const target = desks[(idx + {int(direction)} + desks.length) % desks.length];
        w.desktops = [target];
        // Follow the window to the new desktop so the user lands
        // where their window went — otherwise voice'd workflows
        // ('send this to next desktop') leave them staring at empty
        // wallpaper.
        workspace.currentDesktop = target;
    }})();
    """
    return _run_script(js)


def send_active_to_screen(n: int) -> bool:
    """Move the active window to screen N (1-indexed). KWin uses
    sendClientToScreen(window, screen-as-int)."""
    js = f"""
    (function() {{
        const w = workspace.activeWindow;
        if (!w) return;
        const idx = {int(n)} - 1;
        if (idx < 0 || idx >= workspace.screens.length) return;
        workspace.sendClientToScreen(w, idx);
    }})();
    """
    return _run_script(js)


def activate_window_by_name(target: str) -> bool:
    """Find the window whose caption + resourceClass best matches the
    user-supplied target (token-overlap scoring), and activate it.
    Returns True iff the script ran — not iff a window was actually
    matched. Caller has no easy way to know server-side match success
    without a callback channel; we accept that and let the user
    repeat themselves with a different target if nothing happens."""
    target = (target or "").strip()
    if not target:
        return False
    target_js = json.dumps(target)
    js = f"""
    (function() {{
        const target = {target_js};
        const targetTokens = (target.toLowerCase().match(/[\\p{{L}}\\p{{N}}]+/gu) || []);
        if (targetTokens.length === 0) return;
        let best = null;
        let bestScore = 0;
        workspace.windowList().forEach(w => {{
            if (!w.normalWindow) return;
            if (w.skipTaskbar) return;
            const haystack = ((w.caption || "") + " " + (w.resourceClass || "")).toLowerCase();
            const haystackTokens = new Set(haystack.match(/[\\p{{L}}\\p{{N}}]+/gu) || []);
            let score = 0;
            targetTokens.forEach(t => {{ if (haystackTokens.has(t)) score++; }});
            // Prefer non-minimized windows on tied scores so a user
            // saying 'focus Firefox' lands on the visible tab rather
            // than a minimized one.
            const tieBreak = w.minimized ? 0 : 0.5;
            const totalScore = score + tieBreak;
            if (totalScore > bestScore) {{ bestScore = totalScore; best = w; }}
        }});
        if (best && bestScore >= 1) {{
            workspace.activeWindow = best;
        }}
    }})();
    """
    return _run_script(js)
