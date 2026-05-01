#!/usr/bin/python3
"""probe-ama-connect.py — RFCOMM connectivity test against the PXC 550 II.

NOTE on shebang: this script intentionally hardcodes /usr/bin/python3 (the
*system* interpreter) rather than `env python3`. Reason: uv-managed Pythons
from python-build-standalone are compiled without <bluetooth/bluetooth.h>
in their build environment, so socket.AF_BLUETOOTH is unavailable. The
distro Python is built against the headers and works.

Phase-1.5 of the Alexa-button feature. Two modes:

    scripts/probe-ama-connect.py scan AA:BB:CC:DD:EE:FF
        → tries RFCOMM channels 1..30, reports which accept connections
          and what (if any) bytes the device emits on connect.

    scripts/probe-ama-connect.py hold AA:BB:CC:DD:EE:FF <channel> [seconds]
        → connects to the named channel, holds open, hex-dumps incoming
          bytes with timestamps. User presses the Alexa button during
          the window — anything that arrives is a button-press frame
          candidate.

Pure stdlib. Linux only (AF_BLUETOOTH / BTPROTO_RFCOMM are kernel-side).

The output of `hold` mode (with Alexa button presses) goes into
docs/headphone-recon.md to design the protobuf-frame parser in Phase 2.
"""
from __future__ import annotations

import errno
import re
import socket
import sys
import time


def _hexdump(data: bytes, indent: str = "    ") -> str:
    """Compact hex+ASCII dump, 16 bytes per row."""
    rows = []
    for off in range(0, len(data), 16):
        chunk = data[off : off + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        rows.append(f"{indent}{off:04x}  {hex_part:<47}  {ascii_part}")
    return "\n".join(rows)


def _open_rfcomm(mac: str, channel: int, connect_timeout: float = 2.0) -> socket.socket:
    """Open an RFCOMM socket to the given device + channel.

    Raises OSError on failure (caller decides how to interpret).
    """
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    s.settimeout(connect_timeout)
    s.connect((mac, channel))
    return s


def _classify(err: OSError) -> str:
    """Human-readable category for connect errors."""
    e = err.errno
    if e == errno.ECONNREFUSED:
        return "refused (no service on this channel)"
    if e == errno.EHOSTUNREACH:
        return "host unreachable (device disconnected?)"
    if e == errno.ETIMEDOUT or isinstance(err, socket.timeout):
        return "timed out"
    if e == errno.EBUSY:
        return "busy (already connected by another process)"
    if e == errno.EACCES or e == errno.EPERM:
        return "permission denied"
    return f"errno={e} ({err.strerror})"


def cmd_scan(mac: str) -> int:
    """Brute-force RFCOMM channels 1..30, report what answers."""
    print(f"Scanning RFCOMM channels on {mac} (channels 1..30)...")
    print("Each attempt has a 1.0 s connect timeout. Total: ~30 s.\n")

    accepted: list[tuple[int, bytes]] = []
    for channel in range(1, 31):
        sys.stdout.write(f"  channel {channel:>2}: ")
        sys.stdout.flush()
        try:
            s = _open_rfcomm(mac, channel, connect_timeout=1.0)
        except OSError as e:
            print(_classify(e))
            continue

        # Connected. Read whatever the device sends in the next 0.5 s.
        s.settimeout(0.5)
        data = b""
        try:
            data = s.recv(4096)
        except socket.timeout:
            data = b""
        except OSError as e:
            data = b""
            print(f"connected then recv-error: {_classify(e)}", end="")
        finally:
            try:
                s.close()
            except OSError:
                pass

        if data:
            print(f"ACCEPTED, {len(data)} bytes on connect")
            print(_hexdump(data, indent="      "))
        else:
            print("ACCEPTED, silent (waiting for our handshake — typical for AMA / vendor SPP)")
        accepted.append((channel, data))

    print()
    if not accepted:
        print("No channels accepted a connection.")
        print("- Headphones must be connected (bluetoothctl info <MAC> | grep Connected).")
        print("- Some firmware refuses RFCOMM until a Smart Control / Alexa-app handshake.")
        return 1

    print(f"Channels that accepted: {[ch for ch, _ in accepted]}")
    print()
    print("NEXT: pick the most likely AMA channel and hold it open while pressing")
    print("      the Alexa button. AMA is *usually* the highest-numbered accepting")
    print("      channel that isn't HFP/HSP (those are typically 1, 2, 3).")
    print("      Try each remaining candidate:")
    for ch, _ in accepted:
        print(f"        scripts/probe-ama-connect.py hold {mac} {ch} 30")
    return 0


def cmd_hold(mac: str, channel: int, seconds: float) -> int:
    """Hold a connection on `channel`, dump anything received with timestamps."""
    print(f"Connecting to {mac}, RFCOMM channel {channel}...")
    try:
        s = _open_rfcomm(mac, channel, connect_timeout=3.0)
    except OSError as e:
        print(f"  failed: {_classify(e)}")
        return 1

    print(f"  Connected. Holding for {seconds:.0f} s.")
    print(f"  >>> Press the Alexa button NOW (and try short/long press, vol+/-, play/pause). <<<")
    print(f"      Any bytes the device emits will be hex-dumped below with timestamps.\n")

    s.settimeout(0.2)
    deadline = time.monotonic() + seconds
    t0 = time.monotonic()
    total_bytes = 0
    last_event_t = None

    while time.monotonic() < deadline:
        try:
            data = s.recv(4096)
        except socket.timeout:
            continue
        except OSError as e:
            print(f"  recv error: {_classify(e)} — connection dropped.")
            break
        if not data:
            print("  remote closed the connection.")
            break

        now = time.monotonic() - t0
        gap = "" if last_event_t is None else f" (+{now - last_event_t:.3f}s since last)"
        last_event_t = now
        total_bytes += len(data)
        print(f"  [t={now:7.3f}s] {len(data)} bytes{gap}")
        print(_hexdump(data, indent="      "))

    try:
        s.close()
    except OSError:
        pass

    print()
    if total_bytes == 0:
        print("Nothing received. Possibilities:")
        print("  • Wrong channel (not AMA). Try the other channels listed by 'scan' mode.")
        print("  • AMA on this device requires a protobuf handshake (Control.GetDeviceInformation)")
        print("    before it sends button events. Phase 2 will need to send that handshake.")
        print("  • Headphones gate AMA behind Smart Control / Alexa-app registration.")
        return 1

    print(f"Total: {total_bytes} bytes received during the {seconds:.0f}s hold window.")
    print("Save this output into docs/headphone-recon.md → 'AMA frame samples' section.")
    return 0


def main(argv: list[str]) -> int:
    if not hasattr(socket, "AF_BLUETOOTH") or not hasattr(socket, "BTPROTO_RFCOMM"):
        print("ERROR: this Python build doesn't expose AF_BLUETOOTH / BTPROTO_RFCOMM.")
        print(f"  using: {sys.executable} (Python {sys.version_info.major}.{sys.version_info.minor})")
        print("  uv-managed Pythons from python-build-standalone are typically built")
        print("  without these symbols. Run with the distro interpreter instead:")
        print(f"    /usr/bin/python3 {argv[0]} {' '.join(argv[1:])}")
        return 3

    MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$", re.IGNORECASE)

    if len(argv) < 3:
        print(__doc__)
        return 2

    cmd = argv[1]
    mac = argv[2]
    if not MAC_RE.match(mac):
        print(f"ERROR: '{mac}' doesn't look like a Bluetooth MAC (AA:BB:CC:DD:EE:FF).")
        return 2

    if cmd == "scan":
        return cmd_scan(mac)
    if cmd == "hold":
        if len(argv) < 4:
            print("ERROR: 'hold' needs a channel number. Usage:")
            print(f"  {argv[0]} hold {mac} <channel> [seconds]")
            return 2
        try:
            channel = int(argv[3])
        except ValueError:
            print(f"ERROR: channel must be an integer (got {argv[3]!r}).")
            return 2
        seconds = float(argv[4]) if len(argv) >= 5 else 30.0
        return cmd_hold(mac, channel, seconds)

    print(f"ERROR: unknown command {cmd!r}. Use 'scan' or 'hold'.")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
