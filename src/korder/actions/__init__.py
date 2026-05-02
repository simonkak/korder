"""Action registry and built-in action modules.

Importing this package self-registers all built-in actions. Add a new action
by creating a module that calls register(...) at import time and adding it
to _BUILTINS below.

Architecture:
- base.py:        Action dataclass + module-level registry + register() helper
- codes.py:       evdev keycode constants
- keys.py:        single-key actions (Enter, Tab, Escape, Backspace)
- shortcuts.py:   modifier+key actions (Ctrl+Backspace, Ctrl+A, ...)
- text_chars.py:  literal characters that go through the text path (newline)
- parser.py:      regex-based op-list builder driven by the registry

Op tuples returned by parser/segmenter:
  ("text",  str)            — type literally
  ("char",  str)             — type literally (newline etc.; alias for text)
  ("key",   int)             — single keycode
  ("combo", list[int])       — keycodes pressed in order, released in reverse
"""
from korder.actions import base, click, keys, media, modes, now_playing, shortcuts, spotify, system, text_chars, web  # noqa: F401  (self-register)
from korder.actions.base import (
    Action,
    all_actions,
    get_action,
    register,
    trigger_phrase_map,
)

__all__ = [
    "Action",
    "register",
    "get_action",
    "all_actions",
    "trigger_phrase_map",
]
