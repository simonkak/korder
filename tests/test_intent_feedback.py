"""Tests for IntentParser's LLM-generated feedback layer — the
generate_confirm_prompt path that produces natural-language
confirmation questions for destructive actions, with caching and
graceful failure handling."""
from __future__ import annotations

import korder.actions  # noqa: F401  (default registrations)
from korder.intent import IntentParser


def _parser_with_completion(completion_returns):
    """Build a parser whose _call_ollama_completion is patched to return
    the given values per call (callable for dynamic, str for fixed,
    list for sequential)."""
    p = IntentParser()
    if callable(completion_returns):
        p._call_ollama_completion = completion_returns
    elif isinstance(completion_returns, list):
        seq = iter(completion_returns)
        p._call_ollama_completion = lambda *_a, **_kw: next(seq, None)
    else:
        p._call_ollama_completion = lambda *_a, **_kw: completion_returns
    return p


def test_generate_confirm_prompt_returns_completion_text():
    parser = _parser_with_completion("Are you sure you want to shut down?")
    text = parser.generate_confirm_prompt("shutdown", locale="en")
    assert text == "Are you sure you want to shut down?"


def test_generate_confirm_prompt_caches_per_action_and_locale():
    calls = []
    def fake(prompt, max_tokens=80):
        calls.append((prompt, max_tokens))
        return "cached question?"
    parser = _parser_with_completion(fake)

    parser.generate_confirm_prompt("shutdown", locale="en")
    parser.generate_confirm_prompt("shutdown", locale="en")
    parser.generate_confirm_prompt("shutdown", locale="en")
    assert len(calls) == 1, "expected single completion call across three lookups"

    # Different locale → fresh call.
    parser.generate_confirm_prompt("shutdown", locale="pl")
    assert len(calls) == 2

    # Different action → fresh call.
    parser.generate_confirm_prompt("reboot", locale="en")
    assert len(calls) == 3


def test_generate_confirm_prompt_returns_none_for_unknown_action():
    parser = _parser_with_completion("should not be returned")
    assert parser.generate_confirm_prompt("does_not_exist", locale="en") is None


def test_generate_confirm_prompt_returns_none_when_completion_fails():
    """LLM call returned None (transient ollama hiccup) — caller falls
    back to the i18n template chain."""
    parser = _parser_with_completion(None)
    assert parser.generate_confirm_prompt("shutdown", locale="en") is None
    # Failed result is NOT cached — next call retries.
    calls = []
    parser._call_ollama_completion = lambda *a, **kw: calls.append(1) or None
    parser.generate_confirm_prompt("shutdown", locale="en")
    assert len(calls) == 1


def test_generate_confirm_prompt_strips_quotes_and_whitespace():
    """Models sometimes wrap the answer in quotes — strip them so the
    OSD doesn't render literal quote marks."""
    parser = _parser_with_completion('  "Confirm shutdown?"  ')
    assert parser.generate_confirm_prompt("shutdown", locale="en") == "Confirm shutdown?"


def test_generate_confirm_prompt_passes_action_description_to_completion():
    """The prompt sent to the LLM must include the action's description
    so the model knows what's being confirmed."""
    received_prompts = []
    def capture(prompt, max_tokens=80):
        received_prompts.append(prompt)
        return "ok?"
    parser = _parser_with_completion(capture)
    parser.generate_confirm_prompt("shutdown", locale="en")
    assert received_prompts, "completion was not called"
    sent = received_prompts[0]
    # The shutdown action's description starts with "Power off the computer."
    assert "Power off the computer" in sent
    # Locale info should be present so the model picks the right language.
    assert "en" in sent.lower() or "english" in sent.lower()


def test_warm_feedback_cache_skips_already_cached():
    """Pre-generation should not redo work for entries already in the
    cache (e.g. populated by an earlier explicit generate call)."""
    calls = []
    def fake(prompt, max_tokens=80):
        calls.append(1)
        return "fresh prompt?"
    parser = _parser_with_completion(fake)
    # Pre-populate one entry.
    parser._feedback_cache[("confirm", "shutdown", "en")] = "already there"

    # warm_feedback_cache is async (daemon thread); join for the test.
    import threading
    pre_threads = {t.ident for t in threading.enumerate()}
    parser.warm_feedback_cache(locale="en")
    new_threads = [t for t in threading.enumerate() if t.ident not in pre_threads]
    for t in new_threads:
        t.join(timeout=2.0)

    # Pre-existing cache entry preserved; reboot and sleep generated fresh.
    assert parser._feedback_cache[("confirm", "shutdown", "en")] == "already there"
    assert len(calls) == 2, f"expected exactly 2 fresh completions, got {len(calls)}"
