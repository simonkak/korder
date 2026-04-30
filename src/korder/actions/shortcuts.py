"""Modifier+key shortcut actions."""
from korder.actions.base import Action, register
from korder.actions.codes import (
    KEY_A,
    KEY_BACKSPACE,
    KEY_DELETE,
    KEY_END,
    KEY_HOME,
    KEY_LCTRL,
    KEY_LSHIFT,
    KEY_Z,
)


register(Action(
    name="delete_word",
    description="Delete the previous word (Ctrl+Backspace)",
    triggers={
        "en": ["delete word"],
        "pl": ["usuń słowo", "skasuj słowo"],
    },
    op_factory=lambda _args: ("combo", [KEY_LCTRL, KEY_BACKSPACE]),
))

register(Action(
    name="delete_word_forward",
    description="Delete the next word (Ctrl+Delete)",
    triggers={
        "en": ["delete next word"],
        "pl": ["usuń następne słowo"],
    },
    op_factory=lambda _args: ("combo", [KEY_LCTRL, KEY_DELETE]),
))

register(Action(
    name="select_all",
    description="Select all (Ctrl+A)",
    triggers={
        "en": ["select all"],
        "pl": ["zaznacz wszystko"],
    },
    op_factory=lambda _args: ("combo", [KEY_LCTRL, KEY_A]),
))

register(Action(
    name="undo",
    description="Undo (Ctrl+Z)",
    triggers={
        "en": ["undo"],
        "pl": ["cofnij"],
    },
    op_factory=lambda _args: ("combo", [KEY_LCTRL, KEY_Z]),
))

register(Action(
    name="select_to_line_start",
    description="Select from cursor to start of line (Shift+Home)",
    triggers={
        "en": ["select line"],
        "pl": ["zaznacz linię"],
    },
    op_factory=lambda _args: ("combo", [KEY_LSHIFT, KEY_HOME]),
))

register(Action(
    name="select_to_line_end",
    description="Select from cursor to end of line (Shift+End)",
    triggers={
        "en": ["select to end"],
        "pl": ["zaznacz do końca"],
    },
    op_factory=lambda _args: ("combo", [KEY_LSHIFT, KEY_END]),
))
