"""Verifies the _InjectWorker.parse_response signal contract — emitted
exactly once per parse, with the LLM's response field as the payload
(possibly empty string).

The empty-string emission is the bug-fix surface: without it, a parse
that comes back with no response field doesn't refresh the main
thread's _last_llm_response, so a previous turn's pending-prompt
("Co chcesz odtworzyć w Spotify?") survives across an auto-stop +
new-session boundary and spuriously surfaces as a 'conversational
answer' on the next mangled utterance. Test guards against
regression in either direction — must emit even when empty, must
not emit twice, must carry the actual value when populated."""
from __future__ import annotations
import os
import sys
import pytest
from unittest.mock import MagicMock


@pytest.fixture(scope="module")
def qapp():
    """QApplication is required for QObject signal/slot machinery even
    when we never spin up an event loop. Offscreen platform means the
    test runs without a display."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _build_worker(*, ops_returned, last_response):
    """Construct an _InjectWorker around a MagicMock injector configured
    to return the given parse ops and last_op_parser_response. The
    worker's run() is called synchronously (no QThread.start) so
    same-thread signal connections fire before run() returns."""
    from korder.ui.main_window import _InjectWorker
    injector = MagicMock()
    injector.parse_ops.return_value = ops_returned
    injector.last_op_parser_response.return_value = last_response
    # Explicitly False/True — MagicMock defaults to truthy on attribute
    # access, which would steer run() down the loading_started branch.
    injector.is_slow_parser = False
    injector.is_op_parser_warm.return_value = True
    return _InjectWorker(
        injector=injector,
        payload="some text ",
        original="some text",
        initial_write_mode=False,
    )


def test_parse_response_emits_populated_value(qapp):
    """Baseline: when the LLM returned a response, the worker emits
    that value verbatim."""
    received: list[str] = []
    worker = _build_worker(
        ops_returned=[("text", "some text")],
        last_response="Co chcesz odtworzyć w Spotify?",
    )
    worker.parse_response.connect(received.append)
    worker.run()
    assert received == ["Co chcesz odtworzyć w Spotify?"]


def test_parse_response_emits_empty_when_llm_omitted_field(qapp):
    """The regression case. LLM returned actions=[] and no response
    field (mangled Whisper input, fallback parse, regex path, etc.).
    The worker MUST still emit parse_response(""). Without this, the
    consumer's _last_llm_response retains its previous value, and a
    stale pending-prompt from an earlier session leaks into a later
    turn as a fake conversational answer."""
    received: list[str] = []
    worker = _build_worker(
        ops_returned=[("text", "some mangled text")],
        last_response="",
    )
    worker.parse_response.connect(received.append)
    worker.run()
    assert received == [""], (
        "parse_response must be emitted even on empty response — "
        "absence of an emission lets stale _last_llm_response leak "
        "across parses (the Spotify-pending-prompt-after-auto-stop bug)"
    )


def test_parse_response_emits_exactly_once(qapp):
    """1:1 contract — one parse, one signal. Multiple emissions would
    cause _on_parse_response to clobber its own value mid-turn;
    zero emissions are the leak case above. Sanity check both
    bounds."""
    received: list[str] = []
    worker = _build_worker(
        ops_returned=[("text", "anything")],
        last_response="some response",
    )
    worker.parse_response.connect(received.append)
    worker.run()
    assert len(received) == 1


def test_parse_response_handles_no_last_response_callable(qapp):
    """Defensive: backends without last_op_parser_response (e.g. an
    older injector implementation that pre-dates the response field)
    should still produce one parse_response("") emission. The
    getattr() lookup with default None must not crash run()."""
    from korder.ui.main_window import _InjectWorker
    injector = MagicMock(spec=["parse_ops", "execute_ops", "is_slow_parser",
                               "is_op_parser_warm"])
    injector.parse_ops.return_value = [("text", "x")]
    injector.is_slow_parser = False
    injector.is_op_parser_warm.return_value = True
    worker = _InjectWorker(
        injector=injector,
        payload="x ",
        original="x",
        initial_write_mode=False,
    )
    received: list[str] = []
    worker.parse_response.connect(received.append)
    worker.run()
    assert received == [""]
