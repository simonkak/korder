"""Single-key actions: press_enter, press_tab, press_escape, press_backspace."""
from korder.actions.base import Action, register
from korder.actions.codes import (
    KEY_BACKSPACE,
    KEY_ENTER,
    KEY_ESCAPE,
    KEY_TAB,
)


register(Action(
    name="press_enter",
    description="Press the Enter key (submit, newline-as-key)",
    triggers={
        "en": ["press enter", "press return"],
        "pl": ["wciśnij enter", "naciśnij enter", "wyślij", "potem enter"],
    },
    op_factory=lambda _args: ("key", KEY_ENTER),
))

register(Action(
    name="press_tab",
    description="Press the Tab key",
    triggers={
        "en": ["press tab"],
        "pl": ["tabuluj", "wciśnij tab", "naciśnij tab"],
    },
    op_factory=lambda _args: ("key", KEY_TAB),
))

register(Action(
    name="press_escape",
    description="Press the Escape key",
    triggers={
        "en": ["press escape", "press esc"],
        "pl": ["wciśnij escape", "naciśnij escape"],
    },
    op_factory=lambda _args: ("key", KEY_ESCAPE),
))

register(Action(
    name="press_backspace",
    description="Press the Backspace key (delete one character before cursor)",
    triggers={
        "en": ["press backspace"],
        "pl": ["skasuj", "wciśnij backspace", "naciśnij backspace"],
    },
    op_factory=lambda _args: ("key", KEY_BACKSPACE),
))
