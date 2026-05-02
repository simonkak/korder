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
    description=(
        "Press the Enter / Return key. Use ONLY for explicit Enter-key "
        "requests — phrasings that name the key or an explicit submit "
        "action. Do NOT fire for bare conversational affirmations "
        "('yes', 'tak', 'ok', 'continue') on their own, even when "
        "context could make them imply 'submit' — the user has to "
        "actually name the action."
    ),
    triggers={
        "en": ["press enter", "press return"],
        "pl": ["wciśnij enter", "naciśnij enter", "wyślij", "potem enter"],
    },
    op_factory=lambda _args: ("key", KEY_ENTER),
))

register(Action(
    name="press_tab",
    description=(
        "Press the Tab key. Use for explicit requests to insert a tab "
        "character or move focus to the next field."
    ),
    triggers={
        "en": ["press tab"],
        "pl": ["tabuluj", "wciśnij tab", "naciśnij tab"],
    },
    op_factory=lambda _args: ("key", KEY_TAB),
))

register(Action(
    name="press_escape",
    description=(
        "Press the Escape key. Use when the user asks to cancel, dismiss, "
        "or close a dialog/menu."
    ),
    triggers={
        "en": ["press escape", "press esc"],
        "pl": ["wciśnij escape", "naciśnij escape"],
    },
    op_factory=lambda _args: ("key", KEY_ESCAPE),
))

register(Action(
    name="press_backspace",
    description=(
        "Press the Backspace key once — deletes one character before the "
        "cursor. Distinct from delete_word (which deletes a whole word)."
    ),
    triggers={
        "en": ["press backspace"],
        "pl": ["skasuj", "wciśnij backspace", "naciśnij backspace"],
    },
    op_factory=lambda _args: ("key", KEY_BACKSPACE),
))
