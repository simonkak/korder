"""Integration tests against a real ollama + Gemma model.

Run normally with `pytest`; they auto-skip if ollama isn't reachable or the
configured model (default gemma4:e4b) isn't pulled. Set
`KORDER_INTEGRATION_MODEL` env to use a different tag.

These exercise the reasoning-first prompt to verify Gemma actually produces
the expected actions for synonyms, Whisper-mistranscriptions, and false
positives — the cases the unit tests can't validate because they mock the
LLM response.
"""
from __future__ import annotations
import pytest

import korder.actions  # noqa: F401  (default registrations)
from korder.actions.codes import (
    KEY_BACKSPACE,
    KEY_ENTER,
    KEY_LCTRL,
    KEY_PLAYPAUSE,
    KEY_STOPCD,
    KEY_VOLUMEDOWN,
    KEY_VOLUMEUP,
)
from korder.intent import IntentParser

pytestmark = pytest.mark.ollama


@pytest.fixture(scope="module")
def parser(integration_model: str) -> IntentParser:
    """A live IntentParser bound to the configured ollama model.
    Module-scoped so the model stays loaded across the test cases below."""
    return IntentParser(
        model=integration_model,
        thinking_mode=False,
        show_triggers_in_prompt=False,
    )


# ---- Synonym recognition (the core user complaint) -----------------------

@pytest.mark.parametrize("phrase", [
    "Wznów",
    "Odtwórz",
    "Wznów odtwarzanie",
    "Pauzuj",
    "wstrzymaj",
    "pause",
    "play music",
    "resume",
])
def test_play_pause_synonyms_resolve_to_play_pause(parser, phrase):
    """Polish + English variations all map to play_pause via reasoning,
    without explicit triggers in the prompt."""
    ops = parser.parse(phrase)
    assert any(op == ("key", KEY_PLAYPAUSE) for op in ops), \
        f"{phrase!r} should produce play_pause; got {ops!r}"


def test_whisper_dropped_letter_still_resolves(parser):
    """Whisper sometimes drops a leading letter; reasoning should still
    figure out 'znów odtwarzanie' means resume playback."""
    ops = parser.parse("znów odtwarzanie")
    assert any(op == ("key", KEY_PLAYPAUSE) for op in ops), \
        f"Whisper-dropped variant should still hit play_pause; got {ops!r}"


# ---- Action distinction --------------------------------------------------

def test_stop_playback_distinct_from_play_pause(parser):
    """'Zatrzymaj odtwarzanie' (completely stop) must NOT be confused with
    play_pause (toggle)."""
    ops = parser.parse("Zatrzymaj odtwarzanie")
    # Should be stop_playback, not play_pause
    assert any(op == ("key", KEY_STOPCD) for op in ops), \
        f"stop_playback expected for explicit halt; got {ops!r}"
    assert not any(op == ("key", KEY_PLAYPAUSE) for op in ops)


# ---- Volume control ------------------------------------------------------

@pytest.mark.parametrize("phrase, expected_key", [
    ("głośniej", KEY_VOLUMEUP),
    ("louder", KEY_VOLUMEUP),
    ("ciszej", KEY_VOLUMEDOWN),
    ("quieter", KEY_VOLUMEDOWN),
])
def test_volume_commands(parser, phrase, expected_key):
    ops = parser.parse(phrase)
    assert any(op == ("key", expected_key) for op in ops), \
        f"{phrase!r} expected ({expected_key}); got {ops!r}"


# ---- False positive avoidance --------------------------------------------

@pytest.mark.parametrize("phrase", [
    "she pressed enter on the keyboard",
    "the new line of code is broken",  # 'new line' as descriptive noun
])
def test_descriptive_prose_does_not_trigger_action(parser, phrase):
    """Prose containing command-related words used non-imperatively should
    NOT fire actions. (Genuinely-ambiguous cases like 'I want to pause and
    think' are excluded — humans would disagree on those too.)"""
    ops = parser.parse(phrase)
    # No key/combo/callable ops — only text
    for op in ops:
        assert op[0] in ("text", "char"), \
            f"{phrase!r} produced unexpected action op {op!r}; expected text only"


# ---- Parameterized actions -----------------------------------------------

def test_spotify_search_extracts_query(parser):
    """Gemma should extract the search target into params.query and not
    include the trigger word ('Spotify') in the query itself."""
    ops = parser.parse("Spotify play Pink Floyd")
    # The op shape is ('callable', <closure>); the query is bound inside
    # the closure but we can verify the op kind and that exactly one
    # non-text op was produced.
    callable_ops = [op for op in ops if op[0] == "callable"]
    pending_ops = [op for op in ops if op[0] == "pending_action"]
    # Either resolved (callable) or pending awaiting query — both indicate
    # Gemma identified spotify_search.
    assert callable_ops or pending_ops, \
        f"Expected spotify_search to be detected; got {ops!r}"


def test_spotify_search_pending_when_query_missing(parser):
    """Bare trigger phrase with no query should yield pending_action."""
    ops = parser.parse("Odtwórz na Spotify")
    pending = [op for op in ops if op == ("pending_action", "spotify_search")]
    assert pending, f"Expected pending_action for spotify_search; got {ops!r}"


# ---- Mode toggles --------------------------------------------------------

def test_pisz_enters_write_mode(parser):
    ops = parser.parse("Pisz")
    assert ("write_mode", True) in ops, f"got {ops!r}"


def test_przestan_exits_write_mode(parser):
    ops = parser.parse("Przestań")
    assert ("write_mode", False) in ops, f"got {ops!r}"


# ---- Multi-action input --------------------------------------------------

def test_press_enter_with_trailing_text(parser):
    ops = parser.parse("press enter and run it")
    # Should produce key + text
    has_enter_key = any(op == ("key", KEY_ENTER) for op in ops)
    has_trailing_text = any(op[0] == "text" for op in ops)
    assert has_enter_key, f"Expected press_enter; got {ops!r}"
    assert has_trailing_text, f"Expected trailing text 'and run it'; got {ops!r}"


# ---- Word-level shortcut -------------------------------------------------

def test_delete_word_shortcut(parser):
    """'Usuń słowo' should fire Ctrl+Backspace, not just Backspace."""
    ops = parser.parse("Usuń słowo")
    expected = ("combo", [KEY_LCTRL, KEY_BACKSPACE])
    assert expected in ops, f"got {ops!r}"
    # Should NOT be plain Backspace
    assert not any(op == ("key", KEY_BACKSPACE) for op in ops)


# ---- Whisper mistranscription tolerance ---------------------------------

@pytest.mark.parametrize("phrase", [
    "Znów odtwarzanie",        # dropped W
    "Znów odstwarzanie",       # dropped W + extra s (dual error)
    "wzów odtwarzanie",        # missing N
    "wznów odtwarzania",       # genitive instead of nominative
])
def test_whisper_polish_corruptions_resolve(parser, phrase):
    """Real Whisper outputs corrupt Polish words in many ways; reasoning
    should infer the intent from context regardless."""
    ops = parser.parse(phrase)
    assert any(op == ("key", KEY_PLAYPAUSE) for op in ops), \
        f"{phrase!r} should still resolve to play_pause; got {ops!r}"


# ---- Polish text fidelity (the LLM must NOT rewrite user text) ----------

def test_polish_text_passed_through_verbatim(parser):
    """When the input is plain dictation containing Polish diacritics,
    the LLM should produce no actions and our slicer should hand back
    the text exactly as transcribed — no Gemma-rewriting of 'stało' to
    'stąpio' style failures."""
    text = "Wczoraj stało się coś niezwykłego, dziękuję bardzo."
    ops = parser.parse(text)
    # Either the LLM returned [] (text-only segment from slicer) or it
    # tried to action-classify but the slicer kept the text verbatim.
    text_segs = [op[1] for op in ops if op[0] == "text"]
    # No segment should mutate the original word forms
    full = " ".join(text_segs)
    for word in ("stało", "niezwykłego", "dziękuję"):
        assert word in full, \
            f"Polish word {word!r} mutated or lost; got segments {text_segs!r}"


# ---- Spotify parameter extraction -- per-kind ----------------------------

@pytest.mark.parametrize("phrase, expected_kind, expected_query_substring", [
    ("Spotify zagraj album Linkin Park", "album", "Linkin Park"),
    ("Spotify zagraj utwór Numb", "track", "Numb"),
    ("Spotify play Pink Floyd", "album", "Pink Floyd"),  # default kind
])
def test_spotify_search_kind_inference(parser, phrase, expected_kind, expected_query_substring):
    """Gemma should infer kind=album/track from explicit cues and default to
    album otherwise, with the bare query (no Spotify trigger word)."""
    # We can't directly inspect the closure args from outside, so re-call
    # _call_ollama and inspect the structured output.
    raw = parser._call_ollama(phrase)
    assert raw, f"LLM returned no actions for {phrase!r}"
    spotify = next((a for a in raw if a.get("name") == "spotify_search"), None)
    assert spotify is not None, f"No spotify_search in {raw!r}"
    params = spotify.get("params") or {}
    assert params.get("kind") == expected_kind, \
        f"{phrase!r}: expected kind={expected_kind!r}, got {params!r}"
    assert expected_query_substring.lower() in (params.get("query") or "").lower(), \
        f"{phrase!r}: query missing {expected_query_substring!r}, got {params!r}"
    # Trigger word should not leak into the query.
    assert "spotify" not in (params.get("query") or "").lower()


# ---- Multi-action ordering ----------------------------------------------

def test_multiple_actions_preserved_in_order(parser):
    """Two distinct actions in one utterance should both fire, in order."""
    ops = parser.parse("press enter and turn it down")
    action_ops = [op for op in ops if op[0] in ("key", "combo", "callable")]
    assert any(op == ("key", KEY_ENTER) for op in action_ops), \
        f"Expected press_enter; got {ops!r}"
    assert any(op == ("key", KEY_VOLUMEDOWN) for op in action_ops), \
        f"Expected volume_down; got {ops!r}"
    # Ordering: enter before volume_down (matches utterance order)
    enter_idx = next(i for i, op in enumerate(action_ops) if op == ("key", KEY_ENTER))
    voldown_idx = next(i for i, op in enumerate(action_ops) if op == ("key", KEY_VOLUMEDOWN))
    assert enter_idx < voldown_idx, f"action order wrong: {action_ops!r}"


# ---- Empty / pure-text inputs --------------------------------------------

def test_empty_string_returns_no_ops(parser):
    assert parser.parse("") == []


def test_long_dictation_passes_through_as_text(parser):
    """A multi-sentence dictation block with no commands should produce
    text-only ops, no false action detection."""
    text = (
        "Today I worked on the kitchen renovation, mostly the cabinet "
        "alignment, then took a long walk in the park before dinner."
    )
    ops = parser.parse(text)
    for op in ops:
        assert op[0] in ("text", "char"), \
            f"Long dictation produced unexpected action {op!r}; got {ops!r}"
