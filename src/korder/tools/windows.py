"""Window-list discovery tool.

Wraps ``kwin_bridge.list_windows`` so the LLM can see currently-open
windows when:

- the user wants to focus a specific app / tab and the literal target
  name is needed for ``focus_window`` (this is the action that
  declares ``tools=["list_open_windows"]``); or
- the user asks ABOUT desktop state ("which window is active?",
  "co jest otwarte?", "what's open?") — those are conversational
  questions answered via the ``response`` field, but the LLM still
  needs the current list to answer accurately rather than hallucinate.

Replaces the always-on window-list block that ``IntentParser`` used
to render into every user prompt. With the tool, the round-trip to
KWin only happens when the LLM actually needs the data; the typical
parse (media keys, dictation, system actions) skips it entirely."""
from __future__ import annotations
import logging

from korder.tools.base import Tool, register_tool

log = logging.getLogger(__name__)


def _list_open_windows() -> list[dict]:
    """Returns [{resourceClass, caption, active, minimized}, …] for
    each normal window currently open on the user's KWin session.
    Empty list on any failure — non-Plasma session, KWin not running,
    bridge timeout. The caller (LLM via the loop) sees an empty list
    and decides what to do (often answer "no windows are open" or
    just dispatch without a target)."""
    try:
        from korder import kwin_bridge
    except Exception as e:
        log.warning("list_open_windows: kwin_bridge import failed: %s", e)
        return []
    try:
        raw = kwin_bridge.list_windows(timeout_s=1.0) or []
    except Exception as e:
        log.warning("list_open_windows: bridge call failed: %s", e)
        return []
    out: list[dict] = []
    for w in raw:
        if not isinstance(w, dict):
            continue
        klass = (w.get("resourceClass") or "").strip()
        caption = (w.get("caption") or "").strip()
        if not klass and not caption:
            continue
        out.append({
            "resourceClass": klass,
            "caption": caption,
            "active": bool(w.get("active")),
            "minimized": bool(w.get("minimized")),
        })
    return out


register_tool(Tool(
    name="list_open_windows",
    description=(
        "Enumerate currently-open windows on the user's desktop. "
        "Returns a list of {resourceClass, caption, active, minimized} "
        "entries with the LITERAL window titles and app classes KWin "
        "advertises. Call this when:\n"
        "- the user wants to focus a specific app / tab "
        "(focus_window's target param) and you need the canonical "
        "name to fill it;\n"
        "- the user ASKS about windows ('which window is active?', "
        "'co jest otwarte?', 'what's open right now?') — read the "
        "list and answer in `response`. The 'active' flag marks the "
        "currently-focused window."
    ),
    executor=_list_open_windows,
))
