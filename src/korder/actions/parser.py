"""Regex-based op-list builder driven by the action registry.

Replaces the inline _split_into_ops in inject.py. Compiles a single regex
union from all registered trigger phrases (longer first so multi-word
triggers like "delete word" win over the shorter "delete" alone).
"""
from __future__ import annotations
import re
from functools import lru_cache

from korder.actions.base import get_action, trigger_phrase_map

_PUNCT_BEFORE = " \t.!?;:"
_PUNCT_AFTER = " \t.!?,;:"


@lru_cache(maxsize=1)
def _compile_trigger_regex() -> tuple[re.Pattern, dict[str, str]]:
    """Compile the union pattern of all trigger phrases. Cached so registry
    lookups don't recompile on every transcript. Tests can call
    invalidate_trigger_cache() after registering test actions."""
    phrase_map = trigger_phrase_map()
    if not phrase_map:
        # Empty registry — pattern that matches nothing
        return re.compile(r"(?!)"), {}
    phrases = sorted(phrase_map.keys(), key=len, reverse=True)
    pattern = re.compile(
        r"\b(?:" + "|".join(re.escape(p) for p in phrases) + r")\b",
        re.IGNORECASE,
    )
    return pattern, phrase_map


def invalidate_trigger_cache() -> None:
    """Clear the compiled-regex cache. Call after dynamically registering
    new actions (typically only needed in tests)."""
    _compile_trigger_regex.cache_clear()


def split_into_ops(text: str) -> list[tuple]:
    """Split a transcript into a sequence of op tuples using the action
    registry. Returns an empty list for empty input.

    For parameterized actions (spotify_play, web_search, etc.), the
    text BETWEEN this trigger and the next trigger (or end of input)
    is consumed as the action's first parameter — so "Odtwórz w
    Spotify Linkin Park" via regex routes the same way as via the
    LLM: spotify_play(query="Linkin Park") fires immediately,
    rather than going pending and forcing the user to say the query
    again. Falls through to a pending marker only when the trailing
    region is empty.

    Inner edges (adjacent to action triggers) are stripped of whitespace
    and Whisper-added punctuation. Outer edges (start/end of input) pass
    through untouched so caller-supplied trailing whitespace for
    inter-commit separation is preserved.
    """
    if not text:
        return []
    regex, phrase_map = _compile_trigger_regex()

    matches = list(regex.finditer(text))
    ops: list[tuple] = []
    last_end = 0
    for i, m in enumerate(matches):
        if m.start() > last_end:
            seg = text[last_end:m.start()]
            if last_end > 0:
                seg = seg.lstrip(_PUNCT_AFTER)
            seg = seg.rstrip(_PUNCT_BEFORE)
            if seg:
                ops.append(("text", seg))
        action_name = phrase_map[m.group(0).lower()]
        action = get_action(action_name)
        last_end = m.end()
        if action is None:
            continue
        # Try op_factory with empty args first — distinguishes
        # actions whose parameters are OPTIONAL (volume_up's step_pct
        # has a sensible default; the empty-args op is valid) from
        # actions where the parameter is REQUIRED (spotify_play
        # without query returns None, signaling 'I need the value').
        # Only when op_factory({}) returns None AND the action
        # declares parameters do we treat the trailing text as the
        # parameter value.
        op = action.op_factory({})
        if op is None and action.parameters:
            trailing_start = m.end()
            trailing_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            trailing = (
                text[trailing_start:trailing_end]
                .lstrip(_PUNCT_AFTER)
                .rstrip(_PUNCT_BEFORE)
                .strip()
            )
            if trailing:
                first_param = next(iter(action.parameters.keys()))
                trailing_op = action.op_factory({first_param: trailing})
                if trailing_op is not None:
                    ops.append(trailing_op)
                    last_end = trailing_end
                    continue
            # Empty trailing OR op_factory still rejected — emit
            # pending so MainWindow can grab the next commit as the
            # parameter, same as via the LLM path.
            ops.append(("pending_action", action_name))
        elif op is not None:
            ops.append(op)
    if last_end < len(text):
        seg = text[last_end:]
        if last_end > 0:
            seg = seg.lstrip(_PUNCT_AFTER)
        # No rstrip — preserve outer trailing whitespace.
        if seg:
            ops.append(("text", seg))
    return ops
