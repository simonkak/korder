"""System / desktop-environment level actions.

Things that affect the whole session rather than the focused app:
locking the screen, suspending, shutting down, etc. Most route through
xdg-* / systemctl so they work across DEs without needing KDE-specific
glue.

Power actions (shutdown / reboot / sleep) require explicit voice
confirmation through the pending_action flow — without a 'yes'-class
follow-up word they don't fire, even on a clean trigger match. See
_confirm_op_factory below for the tri-state semantics.
"""
from __future__ import annotations

import logging
import re
import subprocess
from typing import Callable

from korder.actions.base import Action, register
from korder.ui.i18n import t, tf
from korder.ui.progress import emit_progress

log = logging.getLogger(__name__)


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
        log.error("lock_screen: xdg-screensaver failed: %s", e)


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


_YES_WORDS = frozenset({
    "yes", "yeah", "yep", "yup", "ok", "okay", "sure", "confirm", "confirmed",
    "tak", "potwierdzam", "potwierdz", "potwierdź", "okej", "dobrze", "jasne",
})
_NO_WORDS = frozenset({
    "no", "nope", "nah", "negative", "cancel", "stop",
    "nie", "anuluj", "rezygnuję", "rezygnuje", "zatrzymaj",
})


_WORD_RE = re.compile(r"[\w']+", re.UNICODE)


def _is_word(raw: str, vocab: frozenset[str]) -> bool:
    """Match the FIRST alphabetic word in ``raw`` against ``vocab``.

    Tolerates Whisper artifacts: trailing punctuation, repetition
    ('Nie. Nie. Nie. Nie.' from Polish input), wrapping whitespace, and
    leading interjections that the model occasionally inserts. Only
    the first token is considered so a dictated sentence that happens
    to contain a yes/no word later (e.g., 'I had to stop my car')
    doesn't trigger a false confirm/cancel."""
    tokens = _WORD_RE.findall(raw.lower())
    if not tokens:
        return False
    return tokens[0] in vocab


def _make_confirm_op_factory(
    do_action: Callable[[], None],
    cancel_progress_key: str,
):
    """Build an op_factory for a power-state action that requires
    voice confirmation. The returned factory is tri-state:

      - empty/missing confirm → None → action goes pending, OSD asks
        the user for a yes-word.
      - confirm word in _YES_WORDS → returns the dangerous callable.
      - confirm word in _NO_WORDS → returns a benign "cancelled"
        narration callable. The pending state clears, recorder stays
        listening for the next command.
      - any other word → None → main-thread fallthrough re-parses the
        new utterance as a fresh command. Catches the "user changed
        their mind and asked for something else" case without forcing
        an explicit cancel first.
    """
    def factory(args: dict) -> tuple | None:
        raw = (args.get("confirm") or "").strip()
        if not raw:
            return None  # → pending, ask for confirmation
        if _is_word(raw, _YES_WORDS):
            return ("callable", do_action)
        if _is_word(raw, _NO_WORDS):
            return ("callable", lambda: emit_progress(t(cancel_progress_key)))
        # Anything else: the user moved on. Fall through.
        return None
    return factory


def _confirmable_action(
    name: str,
    description: str,
    triggers: dict[str, list[str]],
    runner: Callable[[], None],
    cancel_progress_key: str,
) -> Action:
    return Action(
        name=name,
        description=description,
        triggers=triggers,
        op_factory=_make_confirm_op_factory(runner, cancel_progress_key),
        parameters={
            "confirm": {
                "type": "string",
                "description": (
                    "Explicit confirmation word. Required because this "
                    "action is destructive. Accepted: 'yes' / 'tak' / "
                    "'confirm' / 'potwierdzam' to fire; 'no' / 'nie' / "
                    "'cancel' / 'anuluj' to back out. Fill this when "
                    "the user confirms in the same utterance ('shutdown "
                    "yes', 'wyłącz komputer tak'); leave empty otherwise "
                    "and Korder will ask separately."
                ),
            },
        },
    )


def _do_shutdown() -> None:
    emit_progress(t("progress_shutting_down"))
    try:
        subprocess.run(["systemctl", "poweroff"], check=False, capture_output=True, timeout=5)
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        emit_progress(tf("progress_power_failed", action=t("progress_shutting_down"), error=str(e)))
        log.error("shutdown: systemctl poweroff failed: %s", e)


def _do_reboot() -> None:
    emit_progress(t("progress_rebooting"))
    try:
        subprocess.run(["systemctl", "reboot"], check=False, capture_output=True, timeout=5)
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        emit_progress(tf("progress_power_failed", action=t("progress_rebooting"), error=str(e)))
        log.error("reboot: systemctl reboot failed: %s", e)


def _do_suspend() -> None:
    emit_progress(t("progress_suspending"))
    try:
        subprocess.run(["systemctl", "suspend"], check=False, capture_output=True, timeout=5)
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        emit_progress(tf("progress_power_failed", action=t("progress_suspending"), error=str(e)))
        log.error("sleep: systemctl suspend failed: %s", e)


register(_confirmable_action(
    name="shutdown",
    description=(
        "Power off the computer. Routes through `systemctl poweroff`. "
        "Use ONLY when the user explicitly asks to shut down / power "
        "off / turn off the computer. Do NOT fire on phrases like 'I'll "
        "shut down later' or 'shut down the meeting' — those are "
        "dictation, not commands. Distinct from reboot (restart) and "
        "sleep (suspend). REQUIRES the confirm parameter (see params)."
    ),
    triggers={
        "en": [
            "shutdown computer",
            "shut down computer",
            "shut down the computer",
            "power off computer",
            "power off the computer",
        ],
        "pl": [
            "wyłącz komputer",
            "zamknij system",
            "wyłącz system",
        ],
    },
    runner=_do_shutdown,
    cancel_progress_key="progress_shutdown_cancelled",
))


register(_confirmable_action(
    name="reboot",
    description=(
        "Restart the computer. Routes through `systemctl reboot`. Use "
        "ONLY when the user explicitly asks to restart / reboot the "
        "computer. Do NOT fire on dictation like 'reboot the meeting'. "
        "Distinct from shutdown (full power-off) and sleep (suspend). "
        "REQUIRES the confirm parameter."
    ),
    triggers={
        "en": [
            "reboot computer",
            "reboot the computer",
            "restart computer",
            "restart the computer",
            "restart system",
        ],
        "pl": [
            "uruchom ponownie komputer",
            "zrestartuj komputer",
            "zrestartuj system",
        ],
    },
    runner=_do_reboot,
    cancel_progress_key="progress_reboot_cancelled",
))


register(_confirmable_action(
    name="sleep",
    description=(
        "Suspend the computer to RAM. Routes through `systemctl "
        "suspend`. Use when the user asks to put the computer to "
        "sleep / suspend it. Distinct from shutdown (full power-off) "
        "and reboot (restart). REQUIRES the confirm parameter."
    ),
    triggers={
        "en": [
            "suspend computer",
            "suspend the computer",
            "sleep computer",
            "put computer to sleep",
            "put the computer to sleep",
        ],
        "pl": [
            "uśpij komputer",
            "wstrzymaj komputer",
            "wstrzymaj system",
        ],
    },
    runner=_do_suspend,
    cancel_progress_key="progress_suspend_cancelled",
))


register(Action(
    name="cancel_session",
    description=(
        "End the current dictation/recording session. Drops any pending "
        "text AND any other actions in the same utterance — nothing "
        "gets injected, the OSD hides, the mic closes. Two flavors of "
        "intent map to this: (a) ABORT — 'cancel that', 'nevermind', "
        "'forget it', 'nieważne'; and (b) GRACEFUL END — 'that's all', "
        "'we're done', 'I'm done', 'thanks, that's it', 'koniec', "
        "'zakończ', 'to wszystko', 'to tyle', 'wystarczy'. Both close "
        "the session the same way. Use ONLY when the user's whole "
        "utterance is a meta-end signal — closing remark addressed to "
        "the assistant, not dictated content. Do NOT fire when these "
        "phrases appear inside content (e.g. 'I want to cancel my "
        "subscription' is dictation, not a meta-cancel). When in "
        "doubt, treat it as text — a misfire here drops the whole "
        "utterance and is more disruptive than typing the word."
    ),
    triggers={
        "en": [
            "cancel that",
            "nevermind",
            "never mind",
            "forget it",
            "abort recording",
            "that's all",
            "thats all",
            "we're done",
            "were done",
            "we are done",
            "i'm done",
            "im done",
            "i am done",
            "stop listening",
            "end session",
        ],
        "pl": [
            "nieważne",
            "nie ważne",
            "zapomnij",
            "anuluj nagrywanie",
            "koniec",
            "zakończ",
            "zakończę",
            "to wszystko",
            "to tyle",
            "wystarczy",
            "skończ",
        ],
    },
    op_factory=lambda _args: ("cancel", None),
))
