"""Tests for the JSON Schema response constraint and the streaming
partial-string decoder. No ollama needed — these exercise the pure
Python pieces of the schema builder and streaming machinery."""
from __future__ import annotations

import korder.actions  # noqa: F401  (default registrations)
from korder.intent import (
    IntentParser,
    _build_response_schema,
    _decode_partial_string,
    _params_schema_for_action,
)
from korder.actions.base import get_action


def test_response_schema_top_level_shape():
    schema = _build_response_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"actions", "response", "context"}
    assert schema["required"] == ["actions"]


def test_response_schema_has_one_branch_per_registered_action():
    """The actions array's items.oneOf carries one branch per action,
    each constrained with a const name and the action's params shape.
    No registered action is missing; no extras leak in."""
    from korder.actions.base import all_actions
    schema = _build_response_schema()
    branches = schema["properties"]["actions"]["items"]["oneOf"]
    branch_names = sorted(b["properties"]["name"]["const"] for b in branches)
    expected = sorted(a.name for a in all_actions())
    assert branch_names == expected


def test_confirmable_action_branch_declares_confirm_param():
    """`shutdown` is destructive — its schema must allow `confirm`.
    Non-confirmable actions like `press_enter` must not declare
    `confirm`, so the model can't hallucinate it."""
    schema = _build_response_schema()
    branches = {
        b["properties"]["name"]["const"]: b
        for b in schema["properties"]["actions"]["items"]["oneOf"]
    }
    shutdown_params = branches["shutdown"]["properties"]["params"]["properties"]
    assert "confirm" in shutdown_params

    press_enter_params = branches["press_enter"]["properties"]["params"]["properties"]
    assert "confirm" not in press_enter_params
    # additionalProperties=False is what enforces the no-extras rule.
    assert branches["press_enter"]["properties"]["params"]["additionalProperties"] is False


def test_params_schema_carries_enum_for_constrained_param():
    """spotify_play.kind has an enum — must round-trip into the
    sub-schema so the model can't emit kind='podcast' (not in enum)."""
    spotify = get_action("spotify_play")
    sub = _params_schema_for_action(spotify)
    assert "kind" in sub["properties"]
    assert "enum" in sub["properties"]["kind"]
    assert "track" in sub["properties"]["kind"]["enum"]
    assert sub["additionalProperties"] is False


def test_intent_parser_default_model_is_e4b():
    """E4B is the README's recommended default; constructor must agree."""
    p = IntentParser()
    assert p.model == "gemma4:e4b"


def test_intent_parser_default_schema_mode_on():
    """Schema mode is the safer default. Toggleable for one release
    in case a specific Gemma build chokes on schema-constrained sampling."""
    p = IntentParser()
    assert p.schema_mode is True


def test_format_constraint_picks_schema_when_enabled():
    p = IntentParser(schema_mode=True)
    fmt = p._format_constraint()
    assert isinstance(fmt, dict)
    assert fmt["type"] == "object"


def test_format_constraint_falls_back_to_bare_json_when_disabled():
    p = IntentParser(schema_mode=False)
    fmt = p._format_constraint()
    assert fmt == "json"


# ---- Streaming partial-string decoder ------------------------------------

def test_decode_partial_string_yields_chars_until_close_quote():
    text = '{"actions":[],"response":"Paris."}'
    # value_start is the index after the opening quote of the response value
    value_start = text.index('"response":"') + len('"response":"')
    decoded, cursor, finished = _decode_partial_string(text, value_start, value_start)
    assert decoded == "Paris."
    assert finished is True
    # cursor advanced past the close quote
    assert text[cursor - 1] == '"'


def test_decode_partial_string_handles_partial_input():
    """Mid-stream, the close quote hasn't arrived yet — we should yield
    what we have and report finished=False so the caller waits for more."""
    text = '{"response":"Pari'
    value_start = text.index('"response":"') + len('"response":"')
    decoded, cursor, finished = _decode_partial_string(text, value_start, value_start)
    assert decoded == "Pari"
    assert finished is False
    # next call from the same cursor should resume
    text2 = text + "s."
    decoded2, _, finished2 = _decode_partial_string(text2, value_start, cursor)
    assert decoded2 == "s."
    assert finished2 is False


def test_decode_partial_string_resolves_escape_sequences():
    text = r'{"response":"line1\nline2\""}'
    value_start = text.index('"response":"') + len('"response":"')
    decoded, _, finished = _decode_partial_string(text, value_start, value_start)
    assert decoded == 'line1\nline2"'
    assert finished is True


def test_decode_partial_string_waits_for_split_escape():
    """An escape split across token boundaries shouldn't be consumed
    half-way — the cursor must stay BEFORE the backslash so the next
    chunk can complete the escape."""
    text = '{"response":"abc\\'  # ends with bare backslash, escape incomplete
    value_start = text.index('"response":"') + len('"response":"')
    decoded, cursor, finished = _decode_partial_string(text, value_start, value_start)
    assert decoded == "abc"
    assert finished is False
    # cursor sits at the backslash so the next call sees it intact
    assert text[cursor] == "\\"


# ---- Streaming actions-decision scanner ----------------------------------

def test_scan_actions_decision_detects_empty_array():
    text = '{"actions":[],"response":"hi"}'
    assert IntentParser._scan_actions_decision(text) is False


def test_scan_actions_decision_detects_non_empty_array():
    text = '{"actions":[{"phrase":"x","name":"press_enter"}],"response":""}'
    assert IntentParser._scan_actions_decision(text) is True


def test_scan_actions_decision_returns_none_while_array_open():
    """Mid-stream, the array hasn't closed yet — must return None so
    the caller waits before deciding to stream prose."""
    text = '{"actions":[{"phr'
    assert IntentParser._scan_actions_decision(text) is None


def test_scan_actions_decision_treats_quoted_brackets_as_content():
    """A `]` inside a quoted phrase string must NOT close the array."""
    text = '{"actions":[{"phrase":"x]y","name":"press_enter"}]}'
    assert IntentParser._scan_actions_decision(text) is True
