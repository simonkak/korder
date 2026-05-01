"""Tests for the streaming-partial locked-prefix splitter.

The splitter takes the previous and current Whisper partial strings and
returns (locked, flux): the longest word-aligned common prefix, and the
still-changing tail. The OSD renders the locked region bright and the
flux dim so the eye knows what's settled.
"""
from __future__ import annotations

import pytest

from korder.ui.main_window import _split_at_locked_prefix


def test_empty_prev_locks_nothing():
    assert _split_at_locked_prefix("", "hello world") == ("", "hello world")


def test_clean_word_extension():
    # prev ends on a word boundary (end-of-string), curr extends past it.
    assert _split_at_locked_prefix("what", "what is") == ("what", " is")
    assert _split_at_locked_prefix("what is", "what is the time") == ("what is", " the time")


def test_mid_word_divergence_snaps_back():
    # Whisper said "what is th" then revised to "what is the time".
    # The "th" wasn't a complete word — must stay in flux.
    assert _split_at_locked_prefix("what is th", "what is the time") == ("what is", " the time")


def test_total_replacement():
    # Whisper revised the whole line — no usable lock.
    assert _split_at_locked_prefix("hello", "hi") == ("", "hi")


def test_curr_is_prefix_of_prev_locks_all_of_curr():
    # Buffer shrunk (rare but possible — happens when the model trims
    # trailing punctuation across reruns). All of curr is "settled".
    assert _split_at_locked_prefix("what is the time", "what is") == ("what is", "")


def test_curr_identical_to_prev_locks_all():
    assert _split_at_locked_prefix("hello world", "hello world") == ("hello world", "")


def test_one_char_in_common_does_not_lock_a_partial_word():
    # "hello" vs "hi" share just "h" — no boundary, so nothing locks.
    assert _split_at_locked_prefix("hello", "hi") == ("", "hi")


def test_punctuation_attached_to_word_treated_as_word_part():
    # Whisper sometimes adds trailing punctuation. The punctuation is
    # attached to the previous word; the boundary is still the space.
    locked, flux = _split_at_locked_prefix("hello world", "hello world.")
    # "hello world" matches fully and is at end-of-string in prev,
    # but not at a boundary in curr (curr[11] == '.'). Snap back to the
    # last space at index 5.
    assert locked == "hello"
    assert flux == " world."


def test_trailing_space_in_prev_does_not_break_lock():
    # Whisper sometimes returns trailing whitespace. Should still align.
    locked, flux = _split_at_locked_prefix("what ", "what is")
    assert locked == "what"
    assert flux == " is"


def test_multi_word_divergence_locks_first_settled_words():
    # First two words match, third diverges. Lock the first two.
    locked, flux = _split_at_locked_prefix("the quick brown fox", "the quick red dog")
    assert locked == "the quick"
    assert flux == " red dog"


def test_no_space_in_common_region():
    # First chars match but it's all one word — no boundary to snap to,
    # so flux gets everything.
    assert _split_at_locked_prefix("ab", "abcdef") == ("", "abcdef")


def test_locked_does_not_have_trailing_whitespace():
    # The locked string should never end in whitespace; that goes into
    # the flux side (so the OSD can render the boundary cleanly).
    locked, flux = _split_at_locked_prefix("hello world", "hello world tomorrow")
    assert not locked.endswith(" ")
    assert flux.startswith(" ")
    assert locked + flux == "hello world tomorrow"


def test_locked_plus_flux_always_equals_curr():
    # Invariant: regardless of how the split lands, concat reconstructs curr.
    cases = [
        ("", "anything"),
        ("hello", "hello world"),
        ("what is th", "what is the time"),
        ("hi there", "completely different"),
        ("foo", "foo"),
        ("foo bar", "foo"),
    ]
    for prev, curr in cases:
        locked, flux = _split_at_locked_prefix(prev, curr)
        assert locked + flux == curr, f"{prev!r} → {curr!r} produced {locked!r}+{flux!r}"
