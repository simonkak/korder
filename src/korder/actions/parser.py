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

    Inner edges (adjacent to action triggers) are stripped of whitespace
    and Whisper-added punctuation. Outer edges (start/end of input) pass
    through untouched so caller-supplied trailing whitespace for
    inter-commit separation is preserved.
    """
    if not text:
        return []
    regex, phrase_map = _compile_trigger_regex()

    ops: list[tuple] = []
    last_end = 0
    for m in regex.finditer(text):
        if m.start() > last_end:
            seg = text[last_end:m.start()]
            if last_end > 0:
                seg = seg.lstrip(_PUNCT_AFTER)
            seg = seg.rstrip(_PUNCT_BEFORE)
            if seg:
                ops.append(("text", seg))
        action_name = phrase_map[m.group(0).lower()]
        action = get_action(action_name)
        if action is not None:
            ops.append(action.op_factory({}))
        last_end = m.end()
    if last_end < len(text):
        seg = text[last_end:]
        if last_end > 0:
            seg = seg.lstrip(_PUNCT_AFTER)
        # No rstrip — preserve outer trailing whitespace.
        if seg:
            ops.append(("text", seg))
    return ops
