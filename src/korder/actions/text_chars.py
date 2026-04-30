"""Character-emit actions: insert literal characters via the text path
(typed or pasted) rather than as evdev key events."""
from korder.actions.base import Action, register


register(Action(
    name="new_line",
    description="Insert a newline character (\\n) within typed text",
    triggers={
        "en": ["new line", "newline"],
        "pl": ["nowa linia", "nowy wiersz"],
    },
    op_factory=lambda _args: ("char", "\n"),
))

register(Action(
    name="new_paragraph",
    description="Insert a paragraph break (two newlines)",
    triggers={
        "en": ["new paragraph"],
        "pl": ["nowy akapit"],
    },
    op_factory=lambda _args: ("char", "\n\n"),
))
