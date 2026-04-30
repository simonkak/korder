"""Write-mode toggle actions.

Korder defaults to "preview only" — dictation appears in the OSD but isn't
typed into the focused app. Saying "Pisz" turns on write mode; subsequent
dictation gets injected. "Przestań" turns it back off.

Action ops (keys, shortcuts, media) execute regardless of write mode —
write mode only gates the *typing* of dictated text. Otherwise users
would say "next song" and have it not work because they forgot to enable
write mode, which would be silly.
"""
from korder.actions.base import Action, register


register(Action(
    name="enter_write_mode",
    description=(
        "Begin typing dictated text into the focused application. Use when "
        "the user gives a brief imperative meaning 'start writing' / 'begin "
        "typing' / 'write mode on'."
    ),
    triggers={
        "en": ["start writing", "write mode on"],
        "pl": ["pisz", "włącz pisanie"],
    },
    op_factory=lambda _args: ("write_mode", True),
))

register(Action(
    name="exit_write_mode",
    description=(
        "End typing — return korder to preview-only mode where dictation is "
        "shown but not injected. Use when the user says 'stop' / 'stop writing' / "
        "'cease' / equivalent in their language."
    ),
    triggers={
        "en": ["stop writing", "write mode off"],
        "pl": ["przestań", "przestań pisać", "wyłącz pisanie"],
    },
    op_factory=lambda _args: ("write_mode", False),
))
