"""Tests for the LLM `response` field extraction in IntentParser.
The parser asks Gemma to populate `response` alongside `actions`
when an action requires confirmation; the parser stashes it on
self.last_response for the worker to read."""
from __future__ import annotations
import json
from unittest.mock import patch

import korder.actions  # noqa: F401  (default registrations)
from korder.intent import IntentParser


class _FakeResp:
    """Minimal stand-in for urllib's response object: read() returns a
    bytes payload that mimics ollama's /api/generate JSON shape with
    the LLM's content nested in the `response` field."""

    def __init__(self, llm_output: dict | str):
        if isinstance(llm_output, dict):
            llm_output = json.dumps(llm_output, ensure_ascii=False)
        self._body = json.dumps({"response": llm_output, "thinking": ""}).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patched_parser(llm_output):
    """Build an IntentParser whose underlying urllib.request.urlopen
    returns the given LLM output dict."""
    p = IntentParser()
    return p, patch(
        "korder.intent.urllib.request.urlopen",
        return_value=_FakeResp(llm_output),
    )


def test_last_response_populated_when_llm_supplies_field():
    p, mock_urlopen = _patched_parser({
        "actions": [{"phrase": "wyłącz komputer", "name": "shutdown"}],
        "response": "Czy chcesz wyłączyć komputer? Powiedz tak lub nie.",
    })
    with mock_urlopen:
        p.parse("wyłącz komputer")
    assert p.last_response == "Czy chcesz wyłączyć komputer? Powiedz tak lub nie."


def test_last_response_empty_when_llm_omits_field():
    """Most parses (non-confirmation cases) should leave last_response
    empty so we don't surface a stale message from a previous turn."""
    p, mock_urlopen = _patched_parser({
        "actions": [{"phrase": "press enter", "name": "press_enter"}],
    })
    with mock_urlopen:
        p.parse("press enter")
    assert p.last_response == ""


def test_last_response_resets_between_parses():
    """A second parse with no response field should clear an earlier
    parse's response. Race protection — the worker captures
    last_response immediately after parse_ops returns, but if a stale
    value leaked across turns the wrong hint could surface."""
    p = IntentParser()
    # First parse: confirmation case.
    with patch(
        "korder.intent.urllib.request.urlopen",
        return_value=_FakeResp({
            "actions": [{"phrase": "shutdown", "name": "shutdown"}],
            "response": "Confirm shutdown? say yes or no.",
        }),
    ):
        p.parse("shutdown computer")
    assert p.last_response == "Confirm shutdown? say yes or no."

    # Second parse: plain command, no response field.
    with patch(
        "korder.intent.urllib.request.urlopen",
        return_value=_FakeResp({
            "actions": [{"phrase": "press enter", "name": "press_enter"}],
        }),
    ):
        p.parse("press enter")
    assert p.last_response == ""


def test_last_response_ignores_non_string_field():
    """Defensive: if the LLM emits a non-string for response (number,
    object, null, etc.), we treat it as missing rather than crashing."""
    p, mock_urlopen = _patched_parser({
        "actions": [],
        "response": {"oops": "wrong shape"},
    })
    with mock_urlopen:
        p.parse("hello")
    assert p.last_response == ""


def test_hallucinated_confirm_param_cleared():
    """E2B sometimes returns confirm='true' or confirm='yes' even when
    the user said neither. Without the scrub, op_factory would match
    'yes' against _YES_WORDS and fire systemctl poweroff without real
    consent. With the scrub, confirm gets cleared because 'yes'
    doesn't appear in the input transcript → action goes pending →
    user has a real chance to confirm or cancel."""
    p, mock_urlopen = _patched_parser({
        "actions": [
            {
                "phrase": "uśpij komputer",
                "name": "sleep",
                "params": {"confirm": "yes"},
            }
        ],
    })
    with mock_urlopen:
        ops = p.parse("uśpij komputer")
    # The scrub cleared 'yes' (not in transcript), so op_factory returned
    # None → parser emitted pending_action.
    assert ops == [("pending_action", "sleep")]


def test_legitimate_confirm_param_passes_through():
    """When the user actually said the confirmation word, it stays in
    place and the action fires immediately."""
    p, mock_urlopen = _patched_parser({
        "actions": [
            {
                "phrase": "uśpij komputer tak",
                "name": "sleep",
                "params": {"confirm": "tak"},
            }
        ],
    })
    with mock_urlopen:
        ops = p.parse("uśpij komputer tak")
    # Confirm 'tak' is in the input → scrub leaves it → op_factory returns
    # the systemctl callable → action fires.
    kinds = [op[0] for op in ops]
    assert "callable" in kinds, f"expected the action to fire; got {ops!r}"


def test_last_response_for_conversational_no_action_query():
    """Pure-conversational queries — no action match but the LLM has
    a factual answer — populate `response` and produce no ops besides
    the original text. main_window's _show_conversational_answer
    consumer detects this case (actions empty + last_response set)
    and renders the reply in the OSD instead of the 'didn't get
    that' reset."""
    p, mock_urlopen = _patched_parser({
        "actions": [],
        "response": "Paris.",
    })
    with mock_urlopen:
        ops = p.parse("what is the capital of France")
    assert p.last_response == "Paris."
    # Empty actions → text op for the original transcript (regex
    # supplement runs but won't match either, so just the text).
    assert ("text", "what is the capital of France") in ops


def test_last_response_strips_whitespace():
    """Models sometimes wrap with leading/trailing whitespace —
    consumers display this directly so leading newlines look bad."""
    p, mock_urlopen = _patched_parser({
        "actions": [],
        "response": "  \n  Confirm shutdown?\n\n  ",
    })
    with mock_urlopen:
        p.parse("anything")
    assert p.last_response == "Confirm shutdown?"
