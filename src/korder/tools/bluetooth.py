"""Bluetooth-device discovery tool.

Wraps ``bluetooth._list_devices`` so the LLM can see paired devices
and their connection state during bluetooth_connect / bluetooth_disconnect
parameter resolution."""
from __future__ import annotations
import logging

from korder.actions import bluetooth as bluetooth_action
from korder.tools.base import Tool, register_tool

log = logging.getLogger(__name__)


def _list_paired_bluetooth_devices() -> list[dict]:
    """Returns [{name, mac, connected}, …] for paired BT devices.

    Connection state is computed by intersecting the Paired and
    Connected lists from bluetoothctl. Empty list on any failure."""
    try:
        paired = bluetooth_action._list_devices("Paired")
        connected = {mac for mac, _ in bluetooth_action._list_devices("Connected")}
    except Exception as e:
        log.warning("list_paired_bluetooth_devices: failed: %s", e)
        return []
    return [
        {"name": name, "mac": mac, "connected": (mac in connected)}
        for mac, name in paired
    ]


register_tool(Tool(
    name="list_paired_bluetooth_devices",
    description=(
        "List paired BT devices: [{name, mac, connected}, …]. "
        "USE before bluetooth_connect / bluetooth_disconnect when the "
        "user named a device — pick a literal name. The 'connected' "
        "flag picks which to disconnect when the user is vague. "
        "SKIP for 'disconnect bluetooth' with no name (the action "
        "drops every active link on its own)."
    ),
    executor=_list_paired_bluetooth_devices,
))
