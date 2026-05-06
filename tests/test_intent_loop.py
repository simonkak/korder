"""Tests for the iterative tool-call loop in IntentParser.

Mocks urllib.request.urlopen at the module level (same pattern as
tests/test_intent_response.py) so each iteration can return a
distinct LLM response. Asserts the loop's termination conditions,
repetition guard, iteration cap, and tool-failure isolation.
"""
from __future__ import annotations
import json
from unittest.mock import patch

import pytest

import korder.actions  # noqa: F401  (default registrations)
import korder.tools  # noqa: F401  (default registrations)
from korder.intent import IntentParser, _MAX_TOOL_ITERATIONS
from korder.tools.base import Tool, all_tools, register_tool, reset


class _FakeResp:
    """Minimal stand-in for urllib's response object — same shape
    test_intent_response.py uses."""
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


def _build_responses(*outputs):
    """Turn a sequence of LLM outputs into a urlopen side_effect that
    returns each in turn."""
    iter_responses = iter(_FakeResp(o) for o in outputs)
    return lambda *args, **kwargs: next(iter_responses)


@pytest.fixture
def stub_tool():
    """Register a stub tool that records every call and returns a
    deterministic result. Cleared after the test."""
    saved = list(all_tools())
    reset()
    calls: list[tuple[str, dict]] = []

    def executor(**kwargs):
        calls.append(("stub_tool", dict(kwargs)))
        return [{"name": "alpha"}, {"name": "beta"}]

    register_tool(Tool(
        name="stub_tool",
        description="Stub tool for tests",
        executor=executor,
    ))
    yield calls
    reset()
    for tool in saved:
        register_tool(tool)


# ---- happy-path iteration ------------------------------------------------


def test_loop_runs_tool_then_dispatches_action(stub_tool):
    """Iteration 1: LLM emits tool_calls only. Korder runs the tool
    and feeds results back. Iteration 2: LLM emits actions, the loop
    terminates, dispatch happens."""
    p = IntentParser()
    side_effect = _build_responses(
        # Iteration 1 — request the tool
        {"actions": [], "tool_calls": [{"name": "stub_tool", "args": {}}]},
        # Iteration 2 — emit an action; pick press_enter so we don't
        # need a parametric action's params for this test.
        {"actions": [{"phrase": "press enter", "name": "press_enter"}]},
    )
    with patch("korder.intent.urllib.request.urlopen", side_effect=side_effect):
        ops = p.parse("press enter")
    assert stub_tool == [("stub_tool", {})], (
        f"expected stub_tool to run once with empty args; got {stub_tool!r}"
    )
    # press_enter dispatched (keycode 28 per actions/codes.py).
    assert ("key", 28) in ops


# ---- single-pass fast path ----------------------------------------------


def test_loop_skips_iterations_when_actions_emitted_immediately(stub_tool):
    """LLM emits actions on iteration 1 with no tool_calls. The loop
    should not re-call the LLM — common case for media keys, dictation,
    shortcuts."""
    p = IntentParser()
    call_count = [0]

    def side_effect(*args, **kwargs):
        call_count[0] += 1
        return _FakeResp({
            "actions": [{"phrase": "press enter", "name": "press_enter"}],
        })

    with patch("korder.intent.urllib.request.urlopen", side_effect=side_effect):
        p.parse("press enter")
    assert call_count[0] == 1, (
        f"loop must not iterate when no tool_calls present; called {call_count[0]} time(s)"
    )
    assert stub_tool == [], "no tool should have been executed on the fast path"


def test_loop_skips_when_response_emitted(stub_tool):
    """Conversational answer (response non-empty, actions empty,
    tool_calls empty) terminates the loop after iteration 1."""
    p = IntentParser()
    call_count = [0]

    def side_effect(*args, **kwargs):
        call_count[0] += 1
        return _FakeResp({"actions": [], "response": "Paris.", "context": "France"})

    with patch("korder.intent.urllib.request.urlopen", side_effect=side_effect):
        p.parse("what's the capital of France")
    assert call_count[0] == 1
    assert p.last_response == "Paris."


# ---- repetition guard ---------------------------------------------------


def test_loop_breaks_on_repeated_tool_call(stub_tool):
    """If the LLM asks for the same tool with the same args twice in
    one parse, the loop must break — Gemma is stuck."""
    p = IntentParser()
    side_effect = _build_responses(
        # Iteration 1 — request stub_tool
        {"actions": [], "tool_calls": [{"name": "stub_tool", "args": {}}]},
        # Iteration 2 — request the SAME tool again. Repetition guard
        # should fire and break the loop.
        {"actions": [], "tool_calls": [{"name": "stub_tool", "args": {}}]},
    )
    with patch("korder.intent.urllib.request.urlopen", side_effect=side_effect):
        ops = p.parse("hello")
    # The tool ran ONCE (iteration 1). Iteration 2's identical request
    # was caught by the guard.
    assert len(stub_tool) == 1
    # No actions to dispatch — falls through to text.
    assert all(op[0] == "text" for op in ops)


def test_loop_continues_on_distinct_tool_calls(stub_tool):
    """Different args on the same tool name should NOT trigger the
    repetition guard — the LLM legitimately wanted a different
    answer. Verify via two iterations that both run."""
    p = IntentParser()
    side_effect = _build_responses(
        {"actions": [], "tool_calls": [{"name": "stub_tool", "args": {"x": 1}}]},
        {"actions": [], "tool_calls": [{"name": "stub_tool", "args": {"x": 2}}]},
        {"actions": [{"phrase": "press enter", "name": "press_enter"}]},
    )
    with patch("korder.intent.urllib.request.urlopen", side_effect=side_effect):
        p.parse("press enter")
    assert len(stub_tool) == 2
    assert stub_tool[0][1] == {"x": 1}
    assert stub_tool[1][1] == {"x": 2}


# ---- iteration cap ------------------------------------------------------


def test_loop_breaks_at_max_iterations(stub_tool):
    """If the LLM keeps emitting fresh tool_calls forever, the loop
    must break at _MAX_TOOL_ITERATIONS even without the repetition
    guard firing."""
    p = IntentParser()
    # Generate distinct tool calls (different args each time) to evade
    # the repetition guard, then assert the cap kicks in.
    distinct_iters = [
        {"actions": [], "tool_calls": [{"name": "stub_tool", "args": {"i": i}}]}
        for i in range(_MAX_TOOL_ITERATIONS + 3)
    ]
    side_effect = _build_responses(*distinct_iters)
    with patch("korder.intent.urllib.request.urlopen", side_effect=side_effect):
        p.parse("hello")
    # The cap is the number of LLM calls made — anything beyond that
    # is squelched. Each iteration runs exactly one tool call so the
    # tool was hit MAX_TOOL_ITERATIONS times.
    assert len(stub_tool) == _MAX_TOOL_ITERATIONS


# ---- tool failure isolation --------------------------------------------


def test_loop_continues_when_tool_raises():
    """A tool whose executor raises should not kill the loop. The
    error gets recorded, fed back to the LLM, and the loop continues."""
    saved = list(all_tools())
    reset()
    try:
        register_tool(Tool(
            name="failing_tool",
            description="Always raises",
            executor=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        ))

        p = IntentParser()
        side_effect = _build_responses(
            {"actions": [], "tool_calls": [{"name": "failing_tool", "args": {}}]},
            # The LLM sees the error and dispatches anyway.
            {"actions": [{"phrase": "press enter", "name": "press_enter"}]},
        )
        with patch("korder.intent.urllib.request.urlopen", side_effect=side_effect):
            ops = p.parse("press enter")
        assert ("key", 28) in ops, (
            f"loop should have completed despite tool error; got {ops!r}"
        )
    finally:
        reset()
        for tool in saved:
            register_tool(tool)


def test_loop_handles_unknown_tool():
    """LLM emits a tool_calls entry whose name isn't in the registry.
    Loop logs it and continues — doesn't crash."""
    saved = list(all_tools())
    reset()
    try:
        # Don't register any tools — every tool_calls name is unknown.
        # The schema would normally constrain this but our test bypasses
        # the schema by feeding raw JSON. The LOOP must still cope.
        p = IntentParser()
        side_effect = _build_responses(
            {"actions": [], "tool_calls": [{"name": "nonexistent_tool", "args": {}}]},
            {"actions": [{"phrase": "press enter", "name": "press_enter"}]},
        )
        with patch("korder.intent.urllib.request.urlopen", side_effect=side_effect):
            ops = p.parse("press enter")
        assert ("key", 28) in ops
    finally:
        reset()
        for tool in saved:
            register_tool(tool)


# ---- force-on-skip ------------------------------------------------------


def test_loop_forces_discovery_when_llm_skips_tool_for_bound_action():
    """Field-log scenario: Gemma emitted audio_output_switch with a
    guessed sink_name and NO tool_calls. The runtime should detect
    that the action lists `[tools: list_audio_sinks]`, run that tool
    anyway, and re-prompt so the LLM picks a literal sink name."""
    p = IntentParser()
    iter_responses = iter([
        # Iteration 1 — LLM dispatches without consulting tools.
        _FakeResp({
            "actions": [
                {"phrase": "Przełącz dźwięk na głośnik",
                 "name": "audio_output_switch",
                 "params": {"sink_name": "monitor speaker"}},
            ],
        }),
        # Iteration 2 — after forced discovery, the LLM re-emits
        # with a literal name from the tool result.
        _FakeResp({
            "actions": [
                {"phrase": "Przełącz dźwięk na głośnik",
                 "name": "audio_output_switch",
                 "params": {"sink_name": "Głośniki monitora"}},
            ],
        }),
    ])

    captured_prompts: list[str] = []

    def fake_urlopen(req, *args, **kwargs):
        body = json.loads(req.data.decode("utf-8"))
        captured_prompts.append(body.get("prompt", ""))
        return next(iter_responses)

    # Mock the actual sink list to avoid hitting wpctl.
    with (
        patch("korder.intent.urllib.request.urlopen", side_effect=fake_urlopen),
        patch(
            "korder.actions.audio_output._list_sinks",
            return_value=([(59, "Głośniki monitora"), (95, "Denon DHT-S517")], 95),
        ),
    ):
        ops = p.parse("Przełącz dźwięk na głośnik monitora.")

    # Two iterations: forced after iter 1.
    assert len(captured_prompts) == 2, (
        f"expected 2 LLM calls (iter 1 + forced re-prompt); got {len(captured_prompts)}"
    )
    # Iter 2 prompt must contain the canonical sink list — that's the
    # whole point of the force.
    assert "Głośniki monitora" in captured_prompts[1]
    assert "list_audio_sinks" in captured_prompts[1]
    # Final ops must contain a callable (the audio_output_switch
    # callable factory). The corrected sink_name "Głośniki monitora"
    # is what reaches the action factory in iter 2.
    assert any(op[0] == "callable" for op in ops), (
        f"expected the action to dispatch with corrected params; got {ops!r}"
    )


def test_force_synthesizes_parametric_args_from_action_params():
    """Parametric tools used to be skipped by force-on-skip because
    runtime had no way to synthesize args. Now we name-match: tool
    arg `query` ← action param `query`. For spotify_play emitted with
    {query: 'Numb'} and tools=[search_spotify(query)], the runtime
    forces search_spotify(query='Numb') so the LLM gets results to
    pick from on the next iteration."""
    saved = list(all_tools())
    reset()
    try:
        register_tool(Tool(
            name="zero_arg_stub",
            description="zero-arg",
            executor=lambda: ["alpha"],
        ))
        register_tool(Tool(
            name="parametric_stub",
            description="needs query",
            executor=lambda **kw: [],
            args_schema={"query": {"type": "string"}},
        ))

        from korder.actions.base import Action, register, reset as actions_reset, all_actions as all_actions_
        saved_actions = list(all_actions_())
        actions_reset()
        register(Action(
            name="test_dual_tools",
            description="Test action with both tool kinds",
            triggers={"en": ["test_dual"]},
            op_factory=lambda _args: ("callable", lambda: None),
            parameters={"query": {"type": "string"}},
            tools=["zero_arg_stub", "parametric_stub"],
        ))
        try:
            # With non-empty params containing 'query', BOTH tools
            # are forced — zero-arg with {} and parametric with the
            # synthesized {query: 'Numb'}.
            forced = IntentParser._compute_forced_tool_calls(
                actions=[{"name": "test_dual_tools", "params": {"query": "Numb"}}],
                already_seen=set(),
            )
            by_name = {c["name"]: c["args"] for c in forced}
            assert by_name == {
                "zero_arg_stub": {},
                "parametric_stub": {"query": "Numb"},
            }, f"unexpected forced calls: {forced!r}"

            # With EMPTY action params, parametric synthesis has
            # nothing to feed the tool — skip parametric, still
            # force zero-arg.
            forced = IntentParser._compute_forced_tool_calls(
                actions=[{"name": "test_dual_tools", "params": {}}],
                already_seen=set(),
            )
            forced_names = [c["name"] for c in forced]
            assert "zero_arg_stub" in forced_names
            assert "parametric_stub" not in forced_names, (
                f"parametric tool with no params should not be forced; got {forced!r}"
            )
        finally:
            actions_reset()
            for a in saved_actions:
                register(a)
    finally:
        reset()
        for t in saved:
            register_tool(t)


def test_loop_does_not_force_when_action_has_no_tools():
    """Actions without `tools=[...]` (the vast majority — press_enter,
    media keys, dictation) must NOT trigger a forced second iteration.
    That would tank latency for the common path."""
    p = IntentParser()
    call_count = [0]

    def side_effect(*args, **kwargs):
        call_count[0] += 1
        return _FakeResp({
            "actions": [{"phrase": "press enter", "name": "press_enter"}],
        })

    with patch("korder.intent.urllib.request.urlopen", side_effect=side_effect):
        p.parse("press enter")
    assert call_count[0] == 1, (
        f"non-tool-bound action should single-pass; got {call_count[0]} calls"
    )


# ---- relaxed dispatch when phrase mismatches ---------------------------


def test_relaxed_dispatch_when_phrase_does_not_match_input():
    """Field log: 'Zminimalizuj Firefox' → Gemma emitted phrase='minimize'
    (English) which doesn't appear in the Polish input. Segmenter
    returned None, code used to fall back to regex (which ran
    minimize_window with NO target → minimized active Konsole).

    Now the relaxed-dispatch path picks up the schema-validated
    action name + params even when the phrase can't be located. The
    target survives, KWin gets the by-name minimize call."""
    p = IntentParser()

    def side_effect(*args, **kwargs):
        return _FakeResp({
            "actions": [
                {"phrase": "minimize",
                 "name": "minimize_window",
                 "params": {"target": "Firefox"}},
            ],
        })

    captured: list[str] = []
    with (
        patch("korder.intent.urllib.request.urlopen", side_effect=side_effect),
        patch(
            "korder.kwin.minimize_window_by_name",
            side_effect=lambda t: captured.append(t) or True,
        ),
        patch("korder.kwin.minimize_active_window") as minimize_active,
    ):
        ops = p.parse("Zminimalizuj Firefox.")
        # Run the callable so the by-name dispatch fires.
        for op in ops:
            if op[0] == "callable":
                op[1]()

    assert captured == ["Firefox"], (
        f"expected by-name minimize on Firefox; got captured={captured!r} ops={ops!r}"
    )
    minimize_active.assert_not_called()


# ---- iteration-2 prompt content ----------------------------------------


def test_iteration_2_prompt_contains_tool_history(stub_tool):
    """Verify the user prompt sent on iteration 2 contains the
    rendered "Previous tool calls and results" block — that's what
    lets the LLM ground its action params in real system state."""
    p = IntentParser()
    captured_prompts: list[str] = []

    def make_resp(llm_output):
        return _FakeResp(llm_output)

    iter_responses = iter([
        make_resp({"actions": [], "tool_calls": [{"name": "stub_tool", "args": {}}]}),
        make_resp({"actions": [{"phrase": "press enter", "name": "press_enter"}]}),
    ])

    def fake_urlopen(req, *args, **kwargs):
        # urllib.request.Request — the .data attribute holds the JSON body
        body = json.loads(req.data.decode("utf-8"))
        captured_prompts.append(body.get("prompt", ""))
        return next(iter_responses)

    with patch("korder.intent.urllib.request.urlopen", side_effect=fake_urlopen):
        p.parse("press enter")

    assert len(captured_prompts) == 2
    assert "Previous tool calls and results" not in captured_prompts[0], (
        "iteration 1 should not contain a tool-history block"
    )
    assert "Previous tool calls and results" in captured_prompts[1], (
        "iteration 2 must include the tool-history block"
    )
    assert "stub_tool" in captured_prompts[1]
    # The rendered result list should be present so the LLM can see
    # the names returned by the tool.
    assert "alpha" in captured_prompts[1]
    assert "beta" in captured_prompts[1]
