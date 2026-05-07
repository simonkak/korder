"""bluetooth_connect / bluetooth_disconnect — pair-aware BT device control.

Resolves a spoken device name ("Denon", "PXC", "soundbar") against the
list of paired devices reported by `bluetoothctl devices Paired` via the
same fuzzy token-overlap approach as launcher.py. Common nouns the user
actually speaks map to whichever paired device shares the most tokens
with the query — so "Denon" picks "Denon DHT-S517" out of a multi-device
pairing list without forcing the user to recite the model number.

PipeWire's wireplumber default-policy reroutes audio output to a freshly
connected A2DP sink on its own, so we don't need to call `wpctl
set-default` after the link comes up — `bluetoothctl connect <MAC>` is
enough and the speakers/headphones become the default sink within a
fraction of a second of connection.

bluetoothctl is a Bluez front-end that speaks the BlueZ D-Bus API. We
shell out to it rather than calling D-Bus directly because (a) device
discovery already lives in bluetoothctl's text output and re-implementing
ObjectManager parsing buys us nothing, and (b) the connect/disconnect
verbs handle authentication/agent prompts on Bluez's side that a raw
D-Bus call would have to reproduce. Connect can take several seconds on
a cold link (radio handshake + service discovery), so the timeout is
generous."""
from __future__ import annotations
import logging
import re
import shutil
import subprocess

from korder.actions.base import Action, register
from korder.ui.i18n import t, tf
from korder.ui.progress import emit_progress

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_BLUETOOTHCTL_LIST_TIMEOUT_S = 3.0
_BLUETOOTHCTL_CONNECT_TIMEOUT_S = 20.0
_BLUETOOTHCTL_DISCONNECT_TIMEOUT_S = 8.0

# Output line shape from `bluetoothctl devices [Paired|Connected]`:
#   "Device 94:4F:4C:0B:82:F7 Denon DHT-S517"
# MAC is the second field, name is everything after it. Whitespace-loose.
_DEVICE_LINE_RE = re.compile(
    r"^Device\s+([0-9A-Fa-f:]{17})\s+(.+)$",
)


def _tokens(text: str) -> set[str]:
    return {t for t in (m.lower() for m in _TOKEN_RE.findall(text or "")) if t}


def _list_devices(filter_arg: str = "Paired") -> list[tuple[str, str]]:
    """Returns [(mac, name), …] from `bluetoothctl devices <filter>`.

    `filter_arg` is one of "Paired", "Connected", "Trusted", "Bonded"
    (the verbs bluetoothctl accepts). Returns empty list on any failure
    — caller falls through to a "device not found" narration."""
    if not shutil.which("bluetoothctl"):
        log.warning("bluetooth: bluetoothctl not on PATH")
        return []
    try:
        result = subprocess.run(
            ["bluetoothctl", "devices", filter_arg],
            capture_output=True,
            timeout=_BLUETOOTHCTL_LIST_TIMEOUT_S,
            check=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("bluetooth: list %s failed: %s", filter_arg, e)
        return []
    out: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        m = _DEVICE_LINE_RE.match(line.strip())
        if m:
            out.append((m.group(1), m.group(2).strip()))
    return out


def _resolve_device(query: str, devices: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Pick the paired device whose name shares the most tokens with
    `query`. Same scoring family as launcher.py — meaningful (>=3 char)
    token overlap is the dominant signal. Returns (mac, name) or None
    when nothing reaches the confidence floor.

    Floor: at least one >=3-char token in common. The threshold is
    looser than launcher.py's (>=4) because BT names tend to be short
    brand strings ("PXC", "WH-1000", "JBL") and demanding 4+ chars
    would silently miss them."""
    qtokens = _tokens(query)
    if not qtokens or not devices:
        return None
    best: tuple[int, int, str, str] | None = None
    for mac, name in devices:
        ntokens = _tokens(name)
        common = qtokens & ntokens
        meaningful = sum(1 for t in common if len(t) >= 3)
        if meaningful == 0:
            continue
        score = (meaningful, len(common))
        if best is None or score > (best[0], best[1]):
            best = (meaningful, len(common), mac, name)
    if best is None:
        return None
    return (best[2], best[3])


def _bluetoothctl_action(verb: str, mac: str, timeout_s: float) -> bool:
    """Run `bluetoothctl <verb> <MAC>`. Returns True on rc==0.

    bluetoothctl prints status to stdout regardless of rc; we check the
    return code first and log stdout on failure for diagnosability when
    a connect fails because the device is out of range / off."""
    try:
        result = subprocess.run(
            ["bluetoothctl", verb, mac],
            capture_output=True,
            timeout=timeout_s,
            check=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("bluetooth: %s %s failed: %s", verb, mac, e)
        return False
    if result.returncode != 0:
        log.warning(
            "bluetooth: %s %s rc=%d stdout=%r stderr=%r",
            verb, mac, result.returncode, result.stdout, result.stderr,
        )
        return False
    # bluetoothctl sometimes returns rc=0 even when the connect didn't
    # land (e.g. agent rejected, profile unavailable). The stdout
    # contains "Connection successful" on a true success — check for
    # that before claiming victory.
    if verb == "connect" and "successful" not in result.stdout.lower():
        log.warning("bluetooth: connect rc=0 but no success line: %r", result.stdout)
        return False
    return True


def _do_connect(query: str) -> None:
    """Resolve query → paired device, run `bluetoothctl connect`. If the
    device is already connected, narrate that and return — re-issuing
    `connect` on an active link is harmless but the dwell on "Connecting"
    confuses the user."""
    devices = _list_devices("Paired")
    if not devices:
        emit_progress(t("progress_bt_no_paired"))
        return
    resolved = _resolve_device(query, devices)
    if resolved is None:
        emit_progress(tf("progress_bt_not_found", query=query))
        return
    mac, name = resolved
    connected = {m for m, _ in _list_devices("Connected")}
    if mac in connected:
        emit_progress(tf("progress_bt_already_connected", name=name))
        return
    emit_progress(tf("progress_bt_connecting", name=name))
    if _bluetoothctl_action("connect", mac, _BLUETOOTHCTL_CONNECT_TIMEOUT_S):
        emit_progress(tf("progress_bt_connected", name=name))
    else:
        emit_progress(tf("progress_bt_connect_failed", name=name))


def _do_disconnect(query: str) -> None:
    """Resolve query → connected device, run `bluetoothctl disconnect`.

    Empty query disconnects every currently-connected device — useful
    for "rozłącz bluetooth" with no specific name when the user just
    wants the BT audio off the system sink."""
    connected = _list_devices("Connected")
    if not connected:
        emit_progress(t("progress_bt_none_connected"))
        return
    if not query.strip():
        for mac, name in connected:
            emit_progress(tf("progress_bt_disconnecting", name=name))
            _bluetoothctl_action("disconnect", mac, _BLUETOOTHCTL_DISCONNECT_TIMEOUT_S)
        emit_progress(t("progress_bt_disconnected_all"))
        return
    resolved = _resolve_device(query, connected)
    if resolved is None:
        emit_progress(tf("progress_bt_not_found", query=query))
        return
    mac, name = resolved
    emit_progress(tf("progress_bt_disconnecting", name=name))
    if _bluetoothctl_action("disconnect", mac, _BLUETOOTHCTL_DISCONNECT_TIMEOUT_S):
        emit_progress(tf("progress_bt_disconnected", name=name))
    else:
        emit_progress(tf("progress_bt_disconnect_failed", name=name))


def _connect_op(args: dict) -> tuple | None:
    raw = (args or {}).get("device_name", "")
    if not isinstance(raw, str):
        raw = ""
    raw = raw.strip()
    if not raw:
        return None  # → pending, ask which device
    return ("callable", lambda name=raw: _do_connect(name))


def _disconnect_op(args: dict) -> tuple | None:
    """Disconnect tolerates an empty device_name — drops every active
    BT link. So this op_factory always returns a callable; it never
    goes pending."""
    raw = (args or {}).get("device_name", "")
    if not isinstance(raw, str):
        raw = ""
    return ("callable", lambda name=raw: _do_disconnect(name))


register(Action(
    name="bluetooth_connect",
    description=(
        "Connect to a paired Bluetooth device. Call "
        "list_paired_bluetooth_devices first; put a literal name (or "
        "just the brand: 'Denon' matches 'Denon DHT-S517') into "
        "params.device_name. PipeWire auto-routes audio after connect. "
        "USE for 'connect via bluetooth' / equivalents. "
        "SKIP for redirecting audio between already-available sinks — "
        "that's audio_output_switch."
    ),
    triggers={
        "en": [
            "connect bluetooth",
            "connect via bluetooth",
            "connect over bluetooth",
            "connect to bluetooth",
            "bluetooth connect",
        ],
        "pl": [
            "podłącz bluetooth",
            "podłącz przez bluetooth",
            "połącz bluetooth",
            "połącz przez bluetooth",
            "połącz z bluetooth",
            "bluetooth podłącz",
        ],
    },
    op_factory=_connect_op,
    tools=["list_paired_bluetooth_devices"],
    parameters={
        "device_name": {
            "type": "string",
            "required": True,
            "description": (
                "Free-form Bluetooth device name as the user spoke it. "
                "Examples: 'Denon', 'Sony WH-1000', 'PXC 550', "
                "'soundbar', 'słuchawki', 'głośnik kuchenny'. Korder "
                "fuzzy-matches this against paired devices (output of "
                "`bluetoothctl devices Paired`); partial brand or model "
                "names are fine. Required."
            ),
        },
    },
))


register(Action(
    name="bluetooth_disconnect",
    description=(
        "Disconnect a Bluetooth device. If the user named one, fill "
        "params.device_name (literal name from "
        "list_paired_bluetooth_devices). Empty device_name drops "
        "every active BT link. "
        "USE for 'disconnect bluetooth' / equivalents."
    ),
    triggers={
        "en": [
            "disconnect bluetooth",
            "disconnect from bluetooth",
            "bluetooth disconnect",
        ],
        "pl": [
            "rozłącz bluetooth",
            "rozłącz z bluetooth",
            "bluetooth rozłącz",
            "odłącz bluetooth",
            "odłącz przez bluetooth",
        ],
    },
    op_factory=_disconnect_op,
    tools=["list_paired_bluetooth_devices"],
    parameters={
        "device_name": {
            "type": "string",
            "description": (
                "Optional device name to disconnect. When empty, every "
                "currently-connected BT device is dropped. Same fuzzy "
                "matching as bluetooth_connect."
            ),
        },
    },
))
