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
    description=(
        "Delete the previous WORD before the cursor (Ctrl+Backspace). Use "
        "when the user wants to remove the word they just typed, not just "
        "one character. Distinct from press_backspace, which removes a single character."
    ),
    triggers={
        "en": ["delete word"],
        "pl": ["usuń słowo", "skasuj słowo"],
    },
    op_factory=lambda _args: ("combo", [KEY_LCTRL, KEY_BACKSPACE]),
))

register(Action(
    name="delete_word_forward",
    description=(
        "Delete the word AFTER the cursor (Ctrl+Delete). Less common — use "
        "only when the user explicitly says 'next word' / 'forward word' / equivalent."
    ),
    triggers={
        "en": ["delete next word"],
        "pl": ["usuń następne słowo"],
    },
    op_factory=lambda _args: ("combo", [KEY_LCTRL, KEY_DELETE]),
))

register(Action(
    name="select_all",
    description=(
        "Select all content in the focused field/document (Ctrl+A)."
    ),
    triggers={
        "en": ["select all"],
        "pl": ["zaznacz wszystko"],
    },
    op_factory=lambda _args: ("combo", [KEY_LCTRL, KEY_A]),
))

register(Action(
    name="undo",
    description=(
        "Undo the last action (Ctrl+Z). Use when the user says 'undo', "
        "'revert', 'go back', or equivalent."
    ),
    triggers={
        "en": ["undo"],
        "pl": ["cofnij"],
    },
    op_factory=lambda _args: ("combo", [KEY_LCTRL, KEY_Z]),
))

register(Action(
    name="select_to_line_start",
    description=(
        "Select from the cursor to the start of the current line (Shift+Home). "
        "Use when the user says 'select line' or 'select to beginning'."
    ),
    triggers={
        "en": ["select line"],
        "pl": ["zaznacz linię"],
    },
    op_factory=lambda _args: ("combo", [KEY_LSHIFT, KEY_HOME]),
))

register(Action(
    name="select_to_line_end",
    description=(
        "Select from the cursor to the end of the current line (Shift+End)."
    ),
    triggers={
        "en": ["select to end"],
        "pl": ["zaznacz do końca"],
    },
    op_factory=lambda _args: ("combo", [KEY_LSHIFT, KEY_END]),
))
