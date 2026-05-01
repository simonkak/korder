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
from korder.ui.i18n import t, tf
from korder.ui.progress import emit_progress


def _do_lock_screen() -> None:
    """Lock via xdg-screensaver. The tool is part of xdg-utils and
    delegates to the running screen locker (xss-lock, ksmserver,
    gnome-screensaver, etc.) — works on any DE without us needing to
    know which one."""
    emit_progress(t("progress_locking_screen"))
    try:
        subprocess.run(
            ["xdg-screensaver", "lock"],
            check=False,
            capture_output=True,
            timeout=3,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        emit_progress(tf("progress_lock_failed", error=str(e)))
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


register(Action(
    name="cancel_session",
    description=(
        "Abort the current dictation/recording. Drops any pending text "
        "AND any other actions in the same utterance — nothing gets "
        "injected, the OSD shows 'Cancelled' and hides, the mic closes. "
        "Use ONLY when the user is plainly aborting the recording: "
        "saying just 'cancel', 'nevermind', 'forget it', or similar as "
        "the entire intent. Do NOT fire when 'cancel' / 'nevermind' "
        "appear inside dictated content (e.g., 'I want to cancel my "
        "subscription' is dictation, not a meta-cancel). When in doubt, "
        "treat it as text — a misfire here loses the user's whole "
        "utterance and is more disruptive than typing the word."
    ),
    triggers={
        "en": [
            "cancel that",
            "nevermind",
            "never mind",
            "forget it",
            "abort recording",
        ],
        "pl": [
            "nieważne",
            "nie ważne",
            "zapomnij",
            "anuluj nagrywanie",
        ],
    },
    op_factory=lambda _args: ("cancel", None),
))
