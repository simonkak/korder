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


def test_examples_block_lives_in_system_prompt_not_user_prompt():
    """KV-cache prefix reuse: the static examples block must be part
    of the system prompt (byte-identical across calls), not the per-call
    user prompt where history/transcript would invalidate the cached
    prefill."""
    user_msg = _build_user_prompt("hello", show_triggers=False)
    # Static example markers — must NOT be in the per-call user prompt.
    # ('Examples:' alone collides with action descriptions, so we
    # check for the leading static example utterance instead.)
    assert "Naciśnij Enter." not in user_msg
    assert "Spotify zagraj Linkin Park" not in user_msg
    # And they MUST be in the system prompt.
    assert "Naciśnij Enter." in _SYSTEM_PROMPT
    assert "Spotify zagraj Linkin Park" in _SYSTEM_PROMPT


def test_user_prompt_quotes_transcript_safely():
    """Quotes/special chars in transcript must round-trip via JSON encoding."""
    user_msg = _build_user_prompt('she said "hello"', show_triggers=False)
    # JSON-encoded form: she said \"hello\"
    assert "she said" in user_msg


def test_parameterized_action_shows_param_keys_in_catalogue():
    cat = _render_action_catalogue(show_triggers=False)
    assert "spotify_play" in cat
    assert "params: query" in cat


def test_window_list_block_appears_when_supplied():
    """When kwin_bridge returns windows, the user prompt grows a
    'Current windows:' block so the LLM can pick a target verbatim
    for focus_window / close_window."""
    from korder.intent import _render_window_list
    windows = [
        {"id": "u1", "caption": "PR #27 - Mozilla Firefox", "resourceClass": "firefox", "minimized": False},
        {"id": "u2", "caption": "korder ~ Konsole", "resourceClass": "konsole", "minimized": False},
        {"id": "u3", "caption": "Spotify", "resourceClass": "spotify", "minimized": True},
    ]
    block = _render_window_list(windows)
    assert "Current windows" in block
    assert "firefox" in block
    assert "PR #27" in block
    assert "konsole" in block
    assert "(minimized)" in block  # the spotify entry is minimized


def test_window_list_block_is_empty_when_no_windows():
    from korder.intent import _render_window_list
    assert _render_window_list([]) == ""
    assert _render_window_list(None) == ""  # type: ignore[arg-type]


def test_window_list_skips_unusable_entries():
    """Defensive: if KWin returned a window with no caption AND no
    resourceClass, drop it — there's nothing for the LLM to match
    against."""
    from korder.intent import _render_window_list
    windows = [
        {"id": "u1", "caption": "", "resourceClass": "", "minimized": False},
        {"id": "u2", "caption": "Real Window", "resourceClass": "app", "minimized": False},
    ]
    block = _render_window_list(windows)
    assert "Real Window" in block
    # The empty one should not appear; check there's only one bullet line
    assert block.count("  - ") == 1


def test_user_prompt_injects_windows_block_after_history():
    """Window block sits between history and the transcript so the
    LLM reads context (history + windows) before the input it must
    classify."""
    user_msg = _build_user_prompt(
        "focus Firefox",
        history=None,
        show_triggers=False,
        windows=[
            {"id": "u1", "caption": "GitHub PR", "resourceClass": "firefox", "minimized": False},
        ],
    )
    assert "Current windows" in user_msg
    assert "firefox" in user_msg
    # The transcript line still arrives at the end
    assert user_msg.rstrip().endswith("Output:")
    # Window block must come BEFORE the input line so the LLM sees
    # context first.
    win_idx = user_msg.find("Current windows")
    input_idx = user_msg.find("Input: ")
    assert 0 <= win_idx < input_idx


def test_user_prompt_omits_windows_block_when_list_empty():
    user_msg = _build_user_prompt(
        "press enter", history=None, show_triggers=False, windows=[]
    )
    assert "Current windows" not in user_msg


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
