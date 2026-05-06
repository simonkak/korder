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
        "Enumerate Bluetooth devices the user has previously paired. "
        "Returns a list of {name, mac, connected} entries with the "
        "LITERAL device names BlueZ advertises. Call this before "
        "filling device_name params for bluetooth_connect / "
        "bluetooth_disconnect so you can quote one of these names "
        "verbatim instead of guessing or translating. The 'connected' "
        "flag tells you whether the link is currently active — useful "
        "for picking which device to disconnect when the user is "
        "vague."
    ),
    executor=_list_paired_bluetooth_devices,
))
