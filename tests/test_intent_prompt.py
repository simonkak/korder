"""Tests for the system/user prompt split and the show_triggers_in_prompt flag.
Don't hit ollama — just inspect what _build_user_prompt and
_build_system_prompt produce."""
from __future__ import annotations

import korder.actions  # noqa: F401  (default registrations)
from korder.intent import (
    _build_system_prompt,
    _build_user_prompt,
    _invalidate_system_prompt_cache,
    _render_action_catalogue,
)


def test_system_prompt_describes_role_and_format():
    """System prompt should set the role + JSON shape, no per-call data."""
    _invalidate_system_prompt_cache()
    sp = _build_system_prompt()
    assert "action detector" in sp.lower()
    assert "actions" in sp
    assert "phrase" in sp
    assert "name" in sp


def test_catalogue_without_triggers_lists_only_descriptions():
    """Default: prompt shows action name + description only."""
    cat = _render_action_catalogue(show_triggers=False)
    assert "play_pause" in cat
    assert "Toggle the currently active media playback" in cat
    # Should NOT contain explicit trigger phrases
    assert "pauzuj" not in cat
    assert "wznów" not in cat
    assert "głośniej" not in cat


def test_catalogue_with_triggers_includes_phrasings():
    """Legacy mode: prompt enumerates every trigger phrase."""
    cat = _render_action_catalogue(show_triggers=True)
    assert "play_pause" in cat
    assert "pauzuj" in cat or "pause" in cat
    assert "głośniej" in cat or "louder" in cat


def test_user_prompt_includes_transcript_only():
    """The catalogue moved into the system prompt — user prompt now
    carries just history + windows + transcript per call."""
    user_msg = _build_user_prompt("Wznów odtwarzanie", show_triggers=False)
    assert "Wznów odtwarzanie" in user_msg
    # Catalogue must NOT appear here anymore; it lives in the cached
    # system prompt to keep the KV-cache prefix stable across turns.
    assert "Available actions" not in user_msg
    assert "play_pause:" not in user_msg


def test_examples_block_lives_in_system_prompt_not_user_prompt():
    """KV-cache prefix reuse: the static examples block must be part
    of the system prompt (byte-identical across calls), not the per-call
    user prompt where history/transcript would invalidate the cached
    prefill."""
    _invalidate_system_prompt_cache()
    user_msg = _build_user_prompt("hello", show_triggers=False)
    sp = _build_system_prompt()
    # Static example markers — must NOT be in the per-call user prompt.
    # ('Examples:' alone collides with action descriptions, so we
    # check for the leading static example utterance instead.)
    assert "Naciśnij Enter." not in user_msg
    assert "Spotify zagraj Linkin Park" not in user_msg
    # And they MUST be in the system prompt.
    assert "Naciśnij Enter." in sp
    assert "Spotify zagraj Linkin Park" in sp


def test_action_catalogue_lives_in_system_prompt():
    """The catalogue is now part of the cached system prompt, not the
    per-call user prompt. Caching means Gemma's KV cache fingerprints
    the whole tools-and-rules surface as a stable prefix and only
    re-prefills history + windows + transcript per turn."""
    _invalidate_system_prompt_cache()
    sp = _build_system_prompt()
    assert "Available actions:" in sp
    assert "play_pause:" in sp
    assert "press_enter:" in sp


def test_system_prompt_is_cached_and_byte_stable():
    """Two calls return the same string instance (or at least equal
    bytes) so the KV cache fingerprint is stable."""
    _invalidate_system_prompt_cache()
    a = _build_system_prompt()
    b = _build_system_prompt()
    assert a == b
    assert a is b  # cached instance, not rebuilt
    _invalidate_system_prompt_cache()
    c = _build_system_prompt()
    assert a == c
    assert a is not c  # explicit invalidation rebuilt


def test_user_prompt_quotes_transcript_safely():
    """Quotes/special chars in transcript must round-trip via JSON encoding."""
    user_msg = _build_user_prompt('she said "hello"', show_triggers=False)
    # JSON-encoded form: she said \"hello\"
    assert "she said" in user_msg


def test_parameterized_action_shows_param_keys_in_catalogue():
    cat = _render_action_catalogue(show_triggers=False)
    assert "spotify_play" in cat
    assert "params: query" in cat


def test_user_prompt_no_longer_carries_windows_block():
    """Window-list context migrated from always-on injection to the
    list_open_windows tool. The user prompt should NOT carry any
    'Currently open windows' block on its own — that data is fetched
    on demand by the tool-call loop now."""
    user_msg = _build_user_prompt(
        "focus Firefox",
        history=None,
        show_triggers=False,
    )
    assert "Currently open windows" not in user_msg
    # Confirm no kwarg leaks: passing the old `windows` kwarg should
    # raise rather than silently ignore.
    import pytest
    with pytest.raises(TypeError, match="windows"):
        _build_user_prompt("focus Firefox", history=None, windows=[])  # type: ignore[call-arg]


def test_interrogative_detector_uses_question_mark_only():
    """Universal '?' check, no language-specific word lists. Inputs
    ending with '?' (after trailing punctuation) flag as questions;
    everything else doesn't."""
    from korder.intent import _looks_interrogative
    assert _looks_interrogative("Jak nazywa się aktywne okno?")
    assert _looks_interrogative("which window is active?")
    assert _looks_interrogative("co teraz gra?")
    assert _looks_interrogative("foo? ")  # trailing whitespace
    assert _looks_interrogative("foo?,")  # trailing comma
    # No '?' → not flagged, regardless of language or wording
    assert not _looks_interrogative("which window is active")
    assert not _looks_interrogative("co jest aktywne")
    assert not _looks_interrogative("press enter")
    assert not _looks_interrogative("Pause Spotify")
    assert not _looks_interrogative("")


def test_question_input_gets_hint_prepended():
    """Hint nudges the LLM toward empty-actions/response without
    touching the cached system-prompt catalogue."""
    user_msg = _build_user_prompt(
        "Jak nazywa się aktywne okno?",
        history=None,
    )
    assert "QUESTION" in user_msg
    # Catalogue stays in system prompt either way
    assert "Available actions" not in user_msg


def test_imperative_input_gets_no_hint():
    user_msg = _build_user_prompt("press enter", history=None)
    assert "QUESTION" not in user_msg


def test_action_descriptions_are_language_agnostic():
    """Per the user's request: descriptions should describe intent, not list
    Polish-specific synonyms. (Trigger lists in code stay; just shouldn't be
    leaking into description text now.)"""
    from korder.actions.base import all_actions
    for action in all_actions():
        desc_lower = action.description.lower()
        # No explicit Polish synonyms hard-coded into descriptions
        # (these belong in the triggers dict, not the description)
        for forbidden in ["pauzuj", "wznów", "skasuj słowo", "naciśnij"]:
            assert forbidden not in desc_lower, (
                f"Action {action.name!r} description contains language-"
                f"specific trigger {forbidden!r}; should describe intent only"
            )
