"""Tests for the system/user prompt split and the show_triggers_in_prompt flag.
Don't hit ollama — just inspect what _build_user_prompt and _SYSTEM_PROMPT
produce."""
from __future__ import annotations

import korder.actions  # noqa: F401  (default registrations)
from korder.intent import (
    _SYSTEM_PROMPT,
    _build_user_prompt,
    _render_action_catalogue,
)


def test_system_prompt_describes_role_and_format():
    """System prompt should set the role + JSON shape, no per-call data."""
    assert "action detector" in _SYSTEM_PROMPT.lower()
    assert "actions" in _SYSTEM_PROMPT
    assert "phrase" in _SYSTEM_PROMPT
    assert "name" in _SYSTEM_PROMPT


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


def test_user_prompt_includes_transcript_and_catalogue():
    user_msg = _build_user_prompt("Wznów odtwarzanie", show_triggers=False)
    assert "Available actions" in user_msg
    assert "play_pause" in user_msg
    assert "Wznów odtwarzanie" in user_msg
    assert '"actions"' in user_msg


def test_user_prompt_quotes_transcript_safely():
    """Quotes/special chars in transcript must round-trip via JSON encoding."""
    user_msg = _build_user_prompt('she said "hello"', show_triggers=False)
    # JSON-encoded form: she said \"hello\"
    assert "she said" in user_msg


def test_parameterized_action_shows_param_keys_in_catalogue():
    cat = _render_action_catalogue(show_triggers=False)
    assert "spotify_play" in cat
    assert "params: query" in cat


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
