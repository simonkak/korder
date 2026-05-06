"""Tests for the bluetooth_connect / bluetooth_disconnect actions —
mocks the bluetoothctl subprocess so the tests are independent of the
host's BT stack."""
from __future__ import annotations
from unittest.mock import patch

import korder.actions  # noqa: F401  (default registrations)
from korder.actions import bluetooth
from korder.actions.base import get_action


# ---- _list_devices -------------------------------------------------------


class _Run:
    """Minimal subprocess.CompletedProcess stand-in — `text=True` path."""
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


_PAIRED_OUTPUT = (
    "Device 00:1B:66:E8:90:10 PXC 550-II\n"
    "Device 94:4F:4C:0B:82:F7 Denon DHT-S517\n"
)


def test_list_devices_parses_bluetoothctl_output():
    """Verifies the regex strips the 'Device' prefix and splits MAC
    from device name. Trailing whitespace and blank lines are
    tolerated."""
    with (
        patch("korder.actions.bluetooth.shutil.which", return_value="/usr/bin/bluetoothctl"),
        patch(
            "korder.actions.bluetooth.subprocess.run",
            return_value=_Run(_PAIRED_OUTPUT + "\n  \n"),
        ),
    ):
        devices = bluetooth._list_devices("Paired")
    assert devices == [
        ("00:1B:66:E8:90:10", "PXC 550-II"),
        ("94:4F:4C:0B:82:F7", "Denon DHT-S517"),
    ]


def test_list_devices_returns_empty_when_bluetoothctl_missing():
    with patch("korder.actions.bluetooth.shutil.which", return_value=None):
        assert bluetooth._list_devices("Paired") == []


# ---- _resolve_device -----------------------------------------------------


def test_resolve_device_picks_brand_match():
    """User says 'Denon' → matches 'Denon DHT-S517' on the brand
    token. The PXC entry has zero overlap so it's filtered out."""
    devices = [
        ("00:1B:66:E8:90:10", "PXC 550-II"),
        ("94:4F:4C:0B:82:F7", "Denon DHT-S517"),
    ]
    assert bluetooth._resolve_device("Denon", devices) == (
        "94:4F:4C:0B:82:F7", "Denon DHT-S517",
    )


def test_resolve_device_picks_short_brand_token():
    """3-char tokens still match — the floor is >=3 chars (looser
    than launcher's >=4) because BT names tend to be terse brand
    strings like 'PXC' / 'JBL'."""
    devices = [
        ("00:1B:66:E8:90:10", "PXC 550-II"),
        ("94:4F:4C:0B:82:F7", "Denon DHT-S517"),
    ]
    assert bluetooth._resolve_device("PXC", devices) == (
        "00:1B:66:E8:90:10", "PXC 550-II",
    )


def test_resolve_device_returns_none_on_no_match():
    devices = [
        ("00:1B:66:E8:90:10", "PXC 550-II"),
    ]
    assert bluetooth._resolve_device("Sony", devices) is None
    assert bluetooth._resolve_device("", devices) is None
    assert bluetooth._resolve_device("anything", []) is None


def test_resolve_device_prefers_more_meaningful_overlap():
    """Two candidates share short tokens; the one with the brand-name
    overlap wins because its 'denon' token is meaningful (>=3)."""
    devices = [
        ("AA:00:00:00:00:01", "Denon DHT-S517"),
        ("AA:00:00:00:00:02", "Some Speaker S517"),
    ]
    # 'Denon S517' shares 'denon' + 's517' with #1, only 's517' with #2.
    assert bluetooth._resolve_device("Denon S517", devices) == (
        "AA:00:00:00:00:01", "Denon DHT-S517",
    )


# ---- registration --------------------------------------------------------


def test_bluetooth_actions_registered():
    connect = get_action("bluetooth_connect")
    disconnect = get_action("bluetooth_disconnect")
    assert connect is not None
    assert disconnect is not None
    assert "device_name" in connect.parameters
    assert connect.parameters["device_name"].get("required") is True
    # Disconnect's device_name is optional — empty drops every link.
    assert "device_name" in disconnect.parameters
    assert disconnect.parameters["device_name"].get("required") is not True


def test_connect_op_with_empty_name_returns_pending():
    action = get_action("bluetooth_connect")
    assert action.op_factory({}) is None
    assert action.op_factory({"device_name": ""}) is None
    assert action.op_factory({"device_name": "  "}) is None


def test_disconnect_op_with_empty_name_returns_callable():
    """Disconnect with no name is the 'drop everything' shortcut —
    must NOT go pending."""
    action = get_action("bluetooth_disconnect")
    op = action.op_factory({})
    assert op is not None
    assert op[0] == "callable"


# ---- end-to-end dispatch -------------------------------------------------


def test_connect_dispatches_bluetoothctl_connect():
    """Resolver finds Denon → bluetoothctl connect <MAC> runs."""
    captured: list[list[str]] = []

    def fake_run(cmd, **kw):
        captured.append(list(cmd))
        if cmd[:3] == ["bluetoothctl", "devices", "Paired"]:
            return _Run(_PAIRED_OUTPUT)
        if cmd[:3] == ["bluetoothctl", "devices", "Connected"]:
            return _Run("")  # Denon not currently connected
        if cmd[:2] == ["bluetoothctl", "connect"]:
            return _Run("Connection successful\n", returncode=0)
        return _Run("", returncode=1)

    with (
        patch("korder.actions.bluetooth.shutil.which", return_value="/usr/bin/bluetoothctl"),
        patch("korder.actions.bluetooth.subprocess.run", side_effect=fake_run),
    ):
        action = get_action("bluetooth_connect")
        kind, fn = action.op_factory({"device_name": "Denon"})
        assert kind == "callable"
        fn()

    connect_calls = [c for c in captured if c[:2] == ["bluetoothctl", "connect"]]
    assert connect_calls == [["bluetoothctl", "connect", "94:4F:4C:0B:82:F7"]], (
        f"expected single connect to Denon's MAC; got {captured!r}"
    )


def test_connect_skips_when_already_connected():
    """If the resolved device is already in `devices Connected`, we
    narrate "already connected" and do NOT re-issue connect."""
    captured: list[list[str]] = []

    def fake_run(cmd, **kw):
        captured.append(list(cmd))
        if cmd[:3] == ["bluetoothctl", "devices", "Paired"]:
            return _Run(_PAIRED_OUTPUT)
        if cmd[:3] == ["bluetoothctl", "devices", "Connected"]:
            return _Run("Device 94:4F:4C:0B:82:F7 Denon DHT-S517\n")
        if cmd[:2] == ["bluetoothctl", "connect"]:
            return _Run("Connection successful\n", returncode=0)
        return _Run("", returncode=1)

    with (
        patch("korder.actions.bluetooth.shutil.which", return_value="/usr/bin/bluetoothctl"),
        patch("korder.actions.bluetooth.subprocess.run", side_effect=fake_run),
    ):
        action = get_action("bluetooth_connect")
        _, fn = action.op_factory({"device_name": "Denon"})
        fn()

    assert all(c[:2] != ["bluetoothctl", "connect"] for c in captured), (
        f"connect should be skipped when device is already linked; got {captured!r}"
    )


def test_connect_handles_rc_zero_but_no_success_line():
    """bluetoothctl can return rc=0 even on a silent failure (out of
    range, profile mismatch). The wrapper inspects stdout for the
    'successful' marker and treats its absence as failure."""
    def fake_run(cmd, **kw):
        if cmd[:3] == ["bluetoothctl", "devices", "Paired"]:
            return _Run(_PAIRED_OUTPUT)
        if cmd[:3] == ["bluetoothctl", "devices", "Connected"]:
            return _Run("")
        if cmd[:2] == ["bluetoothctl", "connect"]:
            return _Run("Attempting to connect…\n", returncode=0)
        return _Run("", returncode=1)

    with (
        patch("korder.actions.bluetooth.shutil.which", return_value="/usr/bin/bluetoothctl"),
        patch("korder.actions.bluetooth.subprocess.run", side_effect=fake_run),
    ):
        assert bluetooth._bluetoothctl_action(
            "connect", "94:4F:4C:0B:82:F7", timeout_s=5.0,
        ) is False


def test_disconnect_drops_all_when_no_name():
    """Empty device_name → disconnect every connected device."""
    captured: list[list[str]] = []

    def fake_run(cmd, **kw):
        captured.append(list(cmd))
        if cmd[:3] == ["bluetoothctl", "devices", "Connected"]:
            return _Run(
                "Device 94:4F:4C:0B:82:F7 Denon DHT-S517\n"
                "Device 00:1B:66:E8:90:10 PXC 550-II\n"
            )
        if cmd[:2] == ["bluetoothctl", "disconnect"]:
            return _Run("Successful disconnected\n", returncode=0)
        return _Run("", returncode=1)

    with (
        patch("korder.actions.bluetooth.shutil.which", return_value="/usr/bin/bluetoothctl"),
        patch("korder.actions.bluetooth.subprocess.run", side_effect=fake_run),
    ):
        action = get_action("bluetooth_disconnect")
        _, fn = action.op_factory({})
        fn()

    disconnect_macs = [
        c[2] for c in captured if c[:2] == ["bluetoothctl", "disconnect"]
    ]
    assert sorted(disconnect_macs) == sorted(
        ["94:4F:4C:0B:82:F7", "00:1B:66:E8:90:10"]
    )
