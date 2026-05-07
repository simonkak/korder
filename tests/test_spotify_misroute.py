"""Tests for the Spotify misroute override — the deterministic safety
net that flips play_pause → spotify_play when the LLM ignored the
named-Spotify context (Gemma E4B's Polish verb-prior failure mode)."""
from __future__ import annotations

import korder.actions  # noqa: F401  (registry available)
from korder.intent import _maybe_spotify_misroute_override


def test_polish_play_verb_with_single_word_artist_overrides():
    """Field log: 'Odtwórz Queen w Spotify' → LLM emitted play_pause.
    Override extracts query='Queen' and re-routes to spotify_play."""
    actions = [{"phrase": "Odtwórz", "name": "play_pause"}]
    override = _maybe_spotify_misroute_override(
        "Odtwórz Queen w Spotify", actions,
    )
    assert override is not None
    assert override["name"] == "spotify_play"
    assert override["params"]["query"] == "Queen"
    assert "kind" not in override["params"]


def test_polish_wlacz_verb_with_single_word_artist_overrides():
    """Same shape, different verb (Włącz). Diacritic preserved."""
    actions = [{"phrase": "Włącz moderat", "name": "play_pause"}]
    override = _maybe_spotify_misroute_override(
        "Włącz moderat w Spotify", actions,
    )
    assert override is not None
    assert override["params"]["query"] == "moderat"


def test_polish_kind_cue_zespol_sets_artist():
    """'zespół' → kind=artist, dropped from query. Field log:
    'Odtwórz w Spotify zespół Moderat' should yield query='Moderat',
    kind='artist'."""
    actions = [{"phrase": "Odtworzę…", "name": "play_pause"}]
    override = _maybe_spotify_misroute_override(
        "Odtwórz w Spotify zespół Moderat.", actions,
    )
    assert override is not None
    assert override["params"]["query"] == "Moderat"
    assert override["params"]["kind"] == "artist"


def test_english_kind_cue_band_sets_artist():
    actions = [{"phrase": "Play", "name": "play_pause"}]
    override = _maybe_spotify_misroute_override(
        "Play band Queen on Spotify", actions,
    )
    assert override is not None
    assert override["params"]["query"] == "Queen"
    assert override["params"]["kind"] == "artist"


def test_album_kind_cue_sets_album():
    actions = [{"phrase": "Odtwórz", "name": "play_pause"}]
    override = _maybe_spotify_misroute_override(
        "Odtwórz album Hybrid Theory w Spotify", actions,
    )
    assert override is not None
    assert override["params"]["query"] == "Hybrid Theory"
    assert override["params"]["kind"] == "album"


def test_two_word_artist_preserves_query():
    actions = [{"phrase": "Odtwórz", "name": "play_pause"}]
    override = _maybe_spotify_misroute_override(
        "Odtwórz Pink Floyd w Spotify", actions,
    )
    assert override is not None
    assert override["params"]["query"] == "Pink Floyd"


def test_skipped_when_action_is_already_spotify_play_with_query():
    """LLM got it right — don't override correct dispatch."""
    actions = [{
        "phrase": "Odtwórz Queen w Spotify",
        "name": "spotify_play",
        "params": {"query": "Queen"},
    }]
    override = _maybe_spotify_misroute_override(
        "Odtwórz Queen w Spotify", actions,
    )
    assert override is None


def test_overrides_spotify_play_with_empty_params():
    """LLM picked the right action but emitted empty params — without
    rescue this goes pending and re-prompts the user. Override extracts
    the query from the transcript the same way as the play_pause case."""
    actions = [{"phrase": "X", "name": "spotify_play", "params": {}}]
    override = _maybe_spotify_misroute_override(
        "Odtwórz w Spotify zespół Moderat", actions,
    )
    assert override is not None
    assert override["params"]["query"] == "Moderat"
    assert override["params"]["kind"] == "artist"


def test_skipped_when_spotify_play_already_has_uri():
    """LLM did call search_spotify and picked a URI — don't override
    the deliberately-chosen URI dispatch even if query is empty."""
    actions = [{
        "phrase": "X",
        "name": "spotify_play",
        "params": {"uri": "spotify:track:abc"},
    }]
    override = _maybe_spotify_misroute_override(
        "Spotify play X", actions,
    )
    assert override is None


def test_overrides_when_query_is_not_in_transcript():
    """Field log: 'Odtwórz Moderat w Spotify' → Gemma emitted
    spotify_play{query='Moderator'} (false Polish declension reversal,
    'Moderat' became 'Moderator'). The query 'moderator' isn't in the
    transcript verbatim, so the override re-extracts the real subject
    'Moderat' and dispatches with that."""
    actions = [{
        "phrase": "Odtwórz",
        "name": "spotify_play",
        "params": {"query": "Moderator"},
    }]
    override = _maybe_spotify_misroute_override(
        "Odtwórz Moderat w Spotify", actions,
    )
    assert override is not None
    assert override["params"]["query"] == "Moderat"


def test_skipped_when_query_is_in_transcript():
    """LLM extracted a verbatim subject — don't override correct
    output. Common case: 'Spotify play Linkin Park' →
    spotify_play{query='Linkin Park'}, query is in transcript, leave
    alone."""
    actions = [{
        "phrase": "Spotify play Linkin Park",
        "name": "spotify_play",
        "params": {"query": "Linkin Park"},
    }]
    override = _maybe_spotify_misroute_override(
        "Spotify play Linkin Park", actions,
    )
    assert override is None


def test_skipped_when_query_substring_with_kind_cue_stripped():
    """'Odtwórz album Hybrid Theory w Spotify' → query='Hybrid Theory'
    (LLM correctly stripped the 'album' cue). 'hybrid theory' IS in
    the transcript, so the override leaves it alone — even though the
    LLM-emitted query has fewer tokens than the transcript."""
    actions = [{
        "phrase": "Odtwórz album Hybrid Theory w Spotify",
        "name": "spotify_play",
        "params": {"query": "Hybrid Theory", "kind": "album"},
    }]
    override = _maybe_spotify_misroute_override(
        "Odtwórz album Hybrid Theory w Spotify", actions,
    )
    assert override is None


def test_skipped_when_no_spotify_mention():
    """Bare play_pause without Spotify is a real play_pause request."""
    actions = [{"phrase": "Odtwórz", "name": "play_pause"}]
    override = _maybe_spotify_misroute_override("Odtwórz", actions)
    assert override is None


def test_skipped_when_query_empty_after_strip():
    """'Odtwórz w Spotify' with no subject — only verb + preposition +
    Spotify. Nothing left after stripping, so no override (the LLM's
    play_pause may be right or wrong, but we have nothing to swap to)."""
    actions = [{"phrase": "Odtwórz", "name": "play_pause"}]
    override = _maybe_spotify_misroute_override(
        "Odtwórz w Spotify", actions,
    )
    assert override is None


def test_skipped_when_multiple_actions():
    """Multi-action emissions are too ambiguous — only override on
    the unambiguous single-action case."""
    actions = [
        {"phrase": "Odtwórz", "name": "play_pause"},
        {"phrase": "Spotify", "name": "spotify_play"},
    ]
    override = _maybe_spotify_misroute_override(
        "Odtwórz Spotify Queen", actions,
    )
    assert override is None


def test_skipped_when_action_is_not_play_pause():
    """Other media actions (next_track, stop_playback) aren't part of
    the misroute pattern. Don't override them."""
    actions = [{"phrase": "Stop", "name": "stop_playback"}]
    override = _maybe_spotify_misroute_override(
        "Stop Spotify Queen", actions,
    )
    assert override is None


def test_query_preserves_case_and_diacritics():
    """Whisper transcripts may have mixed case and Polish diacritics —
    preserve them through to params.query."""
    actions = [{"phrase": "Włącz", "name": "play_pause"}]
    override = _maybe_spotify_misroute_override(
        "Włącz Małomiasteczkowy w Spotify", actions,
    )
    assert override is not None
    assert override["params"]["query"] == "Małomiasteczkowy"
