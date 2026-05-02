"""Tests for IntentParser's regex-supplement fallback — the safety net
that runs the regex parser when the LLM returns empty results but the
input does contain a registered trigger phrase. Patches _call_ollama
so we don't hit the real LLM."""
from __future__ import annotations
from unittest.mock import patch

import korder.actions  # noqa: F401  (default registrations)
from korder.intent import IntentParser


def _parser_returning(actions: list) -> IntentParser:
    """Build an IntentParser whose _call_ollama always returns the given
    action list. Lets us control LLM behavior in tests deterministically."""
    p = IntentParser()
    p._call_ollama = lambda _transcript: actions  # type: ignore[method-assign]
    return p


def test_regex_supplement_when_llm_empty_and_trigger_present():
    """LLM said no actions, but 'Przestań' is a registered trigger.
    Regex should kick in and produce the write_mode op."""
    parser = _parser_returning([])
    ops = parser.parse("Przestań.")
    # Regex should produce the write_mode False op (period stripped)
    assert ("write_mode", False) in ops


def test_no_supplement_when_llm_empty_and_no_triggers():
    """Plain text with no triggers — LLM empty + regex empty → just text."""
    parser = _parser_returning([])
    ops = parser.parse("hello world")
    assert ops == [("text", "hello world")]


def test_llm_result_used_when_non_empty():
    """LLM found an action; we use it, not the regex result."""
    parser = _parser_returning(
        [{"phrase": "press enter", "name": "press_enter"}]
    )
    ops = parser.parse("press enter and run it")
    # press_enter == keycode 28
    assert ops == [("key", 28), ("text", "and run it")]


def test_descriptive_prose_stays_as_text_when_llm_correctly_empty():
    """LLM correctly classifies 'she pressed enter on the keyboard' as
    no-action; regex would also return text-only because of word-boundary
    matching, so supplement path doesn't accidentally fire."""
    parser = _parser_returning([])
    text = "she pressed enter on the keyboard"
    ops = parser.parse(text)
    # No write_mode/key/combo/subprocess in the result — just text.
    assert all(op[0] == "text" for op in ops)


def test_pisz_recognized_via_supplement():
    """Symmetric to the Przestań test — Pisz alone with empty LLM result
    should still trigger enter_write_mode through the regex supplement."""
    parser = _parser_returning([])
    ops = parser.parse("Pisz.")
    assert ("write_mode", True) in ops


def test_regex_backstop_when_llm_emits_malformed_actions():
    """E4B sometimes returns malformed shapes like
    `[{'name': 'actions', 'args': []}]` or
    `[{'type': 'action', 'action_type': 'search'}]` — non-empty
    actions, but every entry lacks a usable `phrase` field. Without
    a backstop, segmentation skips them all and ops degenerates to
    text-only, silently typing the user's command. With the
    backstop, regex catches the registered trigger and routes the
    intent. Reported case: 'Odtwórz w Spotify.' → LLM emits the
    malformed bag → regex catches the new Polish Spotify trigger →
    spotify_play goes pending."""
    parser = _parser_returning([{"name": "actions", "args": []}])
    ops = parser.parse("Odtwórz w Spotify.")
    kinds = [op[0] for op in ops]
    assert "pending_action" in kinds or "callable" in kinds, (
        f"expected regex backstop to route Spotify intent; got {ops!r}"
    )


def test_no_backstop_when_malformed_llm_and_no_regex_triggers():
    """Same malformed LLM output but on a transcript with no
    registered trigger phrases — backstop should NOT fabricate an
    action. Falls through to text injection."""
    parser = _parser_returning([{"name": "actions", "args": []}])
    ops = parser.parse("just some plain dictation here")
    assert ops == [("text", "just some plain dictation here")]


def test_backstop_ignored_when_segmentation_already_produced_action():
    """When the LLM returns a properly-shaped action that segmentation
    accepts, the backstop must NOT also fire — otherwise a regex
    trigger inside the same utterance would double-fire."""
    parser = _parser_returning(
        [{"phrase": "press enter", "name": "press_enter"}]
    )
    ops = parser.parse("press enter and run it")
    # press_enter once + trailing text — backstop did not double-up.
    assert ops == [("key", 28), ("text", "and run it")]
