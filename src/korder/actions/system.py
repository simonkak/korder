"""System / desktop-environment level actions.

Things that affect the whole session rather than the focused app:
locking the screen, suspending, etc. Most route through xdg-* tools
so they work across DEs without needing KDE-specific glue.

Currently:
- lock_screen → xdg-screensaver lock (works on any FreeDesktop-spec DE)
"""
from __future__ import annotations

import subprocess

from korder.actions.base import Action, register
from korder.ui.progress import emit_progress


def _do_lock_screen() -> None:
    """Lock via xdg-screensaver. The tool is part of xdg-utils and
    delegates to the running screen locker (xss-lock, ksmserver,
    gnome-screensaver, etc.) — works on any DE without us needing to
    know which one."""
    emit_progress("Locking screen…")
    try:
        subprocess.run(
            ["xdg-screensaver", "lock"],
            check=False,
            capture_output=True,
            timeout=3,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        emit_progress(f"Lock failed: {e}")
        print(f"[korder] lock_screen: xdg-screensaver failed: {e}", flush=True)


register(Action(
    name="lock_screen",
    description=(
        "Lock the screen — same as pressing Meta+L. Use when the user "
        "explicitly asks to lock, or says they're stepping away."
    ),
    triggers={
        "en": [
            "lock screen",
            "lock the screen",
            "lock my computer",
            "lock",
        ],
        "pl": [
            "zablokuj ekran",
            "zablokuj komputer",
            "zablokuj",
        ],
    },
    op_factory=lambda _args: ("callable", _do_lock_screen),
))
