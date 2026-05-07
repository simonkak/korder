"""Audio-output discovery tool.

Wraps the existing ``audio_output._list_sinks`` helper so the LLM
sees a structured list of currently-available PipeWire sinks. Used
during ``audio_output_switch`` parameter resolution: the LLM emits
``tool_calls=[list_audio_sinks]`` on the first turn, sees the canonical
sink names, then fills the action's ``sink_name`` param with one of
them on the next turn.

Returning the data shape the LLM consumes lives here, not inside the
action module, so the action module stays focused on dispatch and
the tool can be reused by future actions (``audio_output_describe``,
``audio_output_route_app``)."""
from __future__ import annotations
import logging

from korder.actions import audio_output
from korder.tools.base import Tool, register_tool

log = logging.getLogger(__name__)


def _list_audio_sinks() -> list[dict]:
    """Returns [{name, is_default}, …] for current PipeWire sinks.

    Empty list on any failure — the loop catches and continues with
    no candidates, which causes the LLM to either ask the user or
    fall back to whatever sink_name was guessed."""
    try:
        sinks, default_id = audio_output._list_sinks()
    except Exception as e:
        log.warning("list_audio_sinks: failed: %s", e)
        return []
    return [
        {"name": name, "is_default": (sid == default_id)}
        for sid, name in sinks
    ]


register_tool(Tool(
    name="list_audio_sinks",
    description=(
        "List PipeWire audio sinks: [{name, is_default}, …]. "
        "USE before audio_output_switch when the user named an output — "
        "pick a literal name from the result. "
        "SKIP for non-audio actions and for utterances that don't name "
        "a sink."
    ),
    executor=_list_audio_sinks,
))
