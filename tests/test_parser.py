"""Regex-based parser tests — covers the path from transcript to op tuples
without hitting any LLM."""
from __future__ import annotations

import korder.actions  # noqa: F401  (ensure default registrations)
from korder.actions.codes import (
    KEY_BACKSPACE,
    KEY_ENTER,
    KEY_HOME,
    KEY_LCTRL,
    KEY_LSHIFT,
)
from korder.actions.parser import split_into_ops


def test_plain_text_passes_through_untouched():
    assert split_into_ops("hello world") == [("text", "hello world")]


def test_empty_input():
    assert split_into_ops("") == []


def test_trailing_whitespace_preserved_for_inter_commit_separation():
    """The caller appends a trailing space between commits; parser must keep it."""
    assert split_into_ops("hello world ") == [("text", "hello world ")]


def test_press_enter_alone():
    assert split_into_ops("press enter") == [("key", KEY_ENTER)]


def test_press_enter_with_trailing_period():
    """Whisper adds a period; should be stripped around the action boundary."""
    assert split_into_ops("Press Enter.") == [("key", KEY_ENTER)]


def test_polish_naciśnij_enter():
    assert split_into_ops("Naciśnij Enter") == [("key", KEY_ENTER)]


def test_text_then_action():
    assert split_into_ops("hello press enter") == [
        ("text", "hello"),
        ("key", KEY_ENTER),
    ]


def test_action_then_text():
    """Text after an action should preserve outer trailing whitespace."""
    assert split_into_ops("press enter and run it") == [
        ("key", KEY_ENTER),
        ("text", "and run it"),
    ]


def test_text_action_text():
    assert split_into_ops("hello press enter and continue") == [
        ("text", "hello"),
        ("key", KEY_ENTER),
        ("text", "and continue"),
    ]


def test_descriptive_prose_does_not_trigger():
    """'Pressed enter' (past tense) should not match the trigger phrase
    'press enter' because of the word boundary."""
    text = "she pressed enter on the keyboard"
    assert split_into_ops(text) == [("text", text)]


def test_skasuj_alone_is_backspace():
    assert split_into_ops("Skasuj") == [("key", KEY_BACKSPACE)]


def test_skasuj_słowo_is_ctrl_backspace_not_backspace():
    """Length-descending sort should match the longer 'skasuj słowo' first."""
    assert split_into_ops("Skasuj słowo") == [("combo", [KEY_LCTRL, KEY_BACKSPACE])]


def test_zaznacz_linię():
    assert split_into_ops("Zaznacz linię") == [("combo", [KEY_LSHIFT, KEY_HOME])]


def test_new_line_is_char_op():
    assert split_into_ops("first new line second") == [
        ("text", "first"),
        ("char", "\n"),
        ("text", "second"),
    ]


def test_polish_dictation_with_diacritics_unchanged():
    text = "Dawno, dawno temu za daleką krainą"
    assert split_into_ops(text) == [("text", text)]


def test_consecutive_actions_no_text_between():
    assert split_into_ops("press tab press enter") == [
        ("key", 15),  # KEY_TAB
        ("key", KEY_ENTER),
    ]


def test_volume_up_uses_wpctl_op():
    """Volume actions emit the system_volume op kind; the inject backend
    runs wpctl directly. KEY_VOLUMEUP keycode path was removed because
    KDE's media-key handler raced with the ducker."""
    ops = split_into_ops("głośniej")
    assert ops == [("system_volume", "up")]


def test_play_music_uses_media_keycode():
    ops = split_into_ops("play music")
    assert ops == [("key", 164)]  # KEY_PLAYPAUSE


def test_next_song_polish():
    ops = split_into_ops("następna piosenka")
    assert ops == [("key", 163)]  # KEY_NEXTSONG


def test_text_then_volume_action():
    ops = split_into_ops("write this then volume down")
    assert ops == [
        ("text", "write this then"),
        ("system_volume", "down"),
    ]


def test_pisz_emits_write_mode_on():
    assert split_into_ops("Pisz") == [("write_mode", True)]


def test_przestań_emits_write_mode_off():
    assert split_into_ops("Przestań") == [("write_mode", False)]


def test_inline_mode_toggle_with_text():
    """Pisz hello world — turn on write mode, then text."""
    ops = split_into_ops("Pisz hello world")
    assert ops == [("write_mode", True), ("text", "hello world")]


def test_dictation_with_both_toggles():
    ops = split_into_ops("Pisz hello world Przestań")
    assert ops == [
        ("write_mode", True),
        ("text", "hello world"),
        ("write_mode", False),
    ]
