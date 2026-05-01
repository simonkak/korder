#!/usr/bin/python3
"""probe-ama-handshake.py — send candidate AMA handshakes and observe responses.

After Alexa-app registration, the PXC 550-II accepts our RFCOMM connection
and sends a greeting, but then stays silent on button press because we
haven't authenticated as an Alexa host. This probe lets us iterate on
what handshake message(s) are needed.

Wire format (from alexa-samples/ama-sample-client/buildControlBuffer.js
and the captured frames in docs/headphone-recon.md):

    [2-byte transport header: 0x00 0x00 — streamId=0 control, 8-bit length]
    [1-byte length]
    [protobuf-serialized ControlEnvelope]

The JS sample uses 0x10 0x00 as the header (version=1); the PXC sends
0x00 0x00 (version=0). Both should be accepted.

Usage:
    /usr/bin/python3 scripts/probe-ama-handshake.py 00:1B:66:E8:90:10

The script:
  1. Connects to RFCOMM ch 19.
  2. Reads the 20-byte greeting and prints it.
  3. Sends GET_DEVICE_INFORMATION request.
  4. Hex-dumps anything the device sends back.
  5. Holds the connection — press the Alexa button to see if the headphones
     now route the press to us (a button frame instead of the voice prompt).
"""
from __future__ import annotations

import ctypes
import os
import socket
import struct
import sys
import time

AF_BLUETOOTH = 31
BTPROTO_RFCOMM = 3
SOCKADDR_RC_SIZE = 10

_libc = ctypes.CDLL("libc.so.6", use_errno=True)


def connect_rfcomm(mac: str, channel: int) -> socket.socket:
    s = socket.socket(AF_BLUETOOTH, socket.SOCK_STREAM, BTPROTO_RFCOMM)
    bdaddr_le = bytes.fromhex(mac.replace(":", ""))[::-1]
    addr = struct.pack("<H6sB", AF_BLUETOOTH, bdaddr_le, channel)
    addr += b"\x00" * (SOCKADDR_RC_SIZE - len(addr))
    if _libc.connect(s.fileno(), addr, SOCKADDR_RC_SIZE) != 0:
        err = ctypes.get_errno()
        s.close()
        raise OSError(err, os.strerror(err))
    return s


# ---------- protobuf hand-encoding (just what we need) ----------

def varint(n: int) -> bytes:
    out = bytearray()
    while n > 0x7f:
        out.append((n & 0x7f) | 0x80)
        n >>= 7
    out.append(n & 0x7f)
    return bytes(out)


def tag(field: int, wire_type: int) -> bytes:
    return varint((field << 3) | wire_type)


def encode_varint_field(field: int, value: int) -> bytes:
    return tag(field, 0) + varint(value)


def encode_message_field(field: int, payload: bytes) -> bytes:
    return tag(field, 2) + varint(len(payload)) + payload


# ---------- AMA frame envelope ----------

# Command values (from alexa-samples/Alexa-Gadgets-Embedded-Sample-Code
# accessories.proto, plus AMA-headphone extras seen in the JS sample).
CMD_GET_DEVICE_INFORMATION = 20
CMD_GET_DEVICE_FEATURES = 28
# Field numbers in ControlEnvelope.payload oneof (matches the Command number).


def control_envelope(command: int, payload_field: int | None = None,
                     payload_bytes: bytes = b"") -> bytes:
    """Build a serialized ControlEnvelope protobuf."""
    out = encode_varint_field(1, command)  # field 1 = command
    if payload_field is not None:
        out += encode_message_field(payload_field, payload_bytes)
    return out


def frame(envelope: bytes) -> bytes:
    """Wrap a ControlEnvelope in the AMA transport frame."""
    if len(envelope) > 0xff:
        raise ValueError("payload too big for 8-bit length; use 16-bit header")
    return b"\x00\x00" + bytes([len(envelope)]) + envelope


# ---------- frame parsing (incoming) ----------

def parse_frame(buf: bytes) -> tuple[dict, bytes]:
    """Return ({version, streamId, is16Bits, length, payload}, leftover_bytes)."""
    if len(buf) < 3:
        return {}, buf
    b0, b1 = buf[0], buf[1]
    is16 = b1 & 0x01
    if is16:
        if len(buf) < 4:
            return {}, buf
        length = (buf[2] << 8) | buf[3]
        offset = 4
    else:
        length = buf[2]
        offset = 3
    if len(buf) < offset + length:
        return {}, buf
    payload = buf[offset:offset + length]
    rest = buf[offset + length:]
    return {
        "version": (b0 >> 4) & 0x0f,
        "streamId": ((b0 & 0x0f) << 1) | ((b1 >> 7) & 0x01),
        "is16Bits": bool(is16),
        "length": length,
        "payload": payload,
    }, rest


def parse_protobuf(buf: bytes, depth: int = 0) -> str:
    """Crude pretty-print of a protobuf message — fields and types only."""
    indent = "  " * depth
    out = []
    pos = 0
    while pos < len(buf):
        # tag varint
        t = 0
        shift = 0
        start = pos
        while pos < len(buf):
            b = buf[pos]
            pos += 1
            t |= (b & 0x7f) << shift
            if not (b & 0x80):
                break
            shift += 7
        else:
            out.append(f"{indent}<truncated tag>")
            break
        field = t >> 3
        wt = t & 0x07
        if wt == 0:  # varint
            v = 0
            shift = 0
            while pos < len(buf):
                b = buf[pos]
                pos += 1
                v |= (b & 0x7f) << shift
                if not (b & 0x80):
                    break
                shift += 7
            out.append(f"{indent}field {field} (varint) = {v}")
        elif wt == 2:  # length-delimited
            ln = 0
            shift = 0
            while pos < len(buf):
                b = buf[pos]
                pos += 1
                ln |= (b & 0x7f) << shift
                if not (b & 0x80):
                    break
                shift += 7
            sub = buf[pos:pos + ln]
            pos += ln
            # Heuristic: if it looks like printable ASCII, treat as string.
            if sub and all(0x20 <= c < 0x7f for c in sub):
                out.append(f"{indent}field {field} (string, {ln}B) = {sub.decode()!r}")
            else:
                out.append(f"{indent}field {field} (message, {ln}B) {{")
                out.append(parse_protobuf(sub, depth + 1))
                out.append(f"{indent}}}")
        else:
            out.append(f"{indent}field {field} (wt={wt}) <skipped>")
            break
    return "\n".join(out)


# ---------- helpers ----------

def hexdump(data: bytes, indent: str = "    ") -> str:
    rows = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        h = " ".join(f"{b:02x}" for b in chunk)
        a = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        rows.append(f"{indent}{off:04x}  {h:<47}  {a}")
    return "\n".join(rows)


def recv_with_timeout(sock: socket.socket, timeout_s: float) -> bytes:
    """Read whatever is available within timeout. Returns possibly multi-frame buffer."""
    sock.settimeout(timeout_s)
    chunks = []
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
            sock.settimeout(0.2)  # short timeout for follow-on bytes
    except OSError:
        pass
    return b"".join(chunks)


# ---------- main flow ----------

def send_frame(sock: socket.socket, label: str, env: bytes) -> None:
    f = frame(env)
    print(f"\n>>> SEND {label}: {f.hex()}")
    print(f"    envelope ({len(env)}B): {env.hex()}")
    print(parse_protobuf(env, depth=2))
    sock.sendall(f)


def consume(sock: socket.socket, label: str, timeout_s: float = 2.0) -> bytes:
    data = recv_with_timeout(sock, timeout_s)
    if not data:
        print(f"<<< {label}: (silence)")
        return data
    print(f"\n<<< RECV {label}: {len(data)}B")
    print(hexdump(data))
    rest = data
    while rest:
        f, rest = parse_frame(rest)
        if not f:
            print(f"  (incomplete frame, {len(rest)}B unparsed)")
            break
        print(f"  frame: streamId={f['streamId']} version={f['version']} "
              f"is16Bits={f['is16Bits']} length={f['length']}")
        if f["streamId"] == 0:
            print("  ControlEnvelope:")
            print(parse_protobuf(f["payload"], depth=2))
        else:
            print(f"  audio/data stream payload: {f['payload'][:32].hex()}...")
    return data


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    mac = argv[1]

    print(f"Connecting to {mac} on RFCOMM ch 19...")
    try:
        sock = connect_rfcomm(mac, 19)
    except OSError as e:
        print(f"  failed: {e}")
        return 1
    print(f"  connected (fd={sock.fileno()})")

    # Step 1: read the greeting.
    consume(sock, "greeting", timeout_s=1.5)

    # Step 2: send the full handshake sequence — every command number that
    # the scan found the device recognizes (other than 102 = button event,
    # which is device→host only).
    #
    # 20 = GetDeviceInformation (returns serial/name/transports/device_type)
    # 21 = GetDeviceConfiguration (returns DeviceConfiguration with capabilities)
    # 23, 24, 31 = recognized; ack with empty Response (probably KeepAlive,
    #              SynchronizeSettings, SynchronizeState — order unknown)
    # 30 = recognized; returns empty submessage at field 8
    handshake_sequence = [
        (20, "GetDeviceInformation"),
        (21, "GetDeviceConfiguration"),
        (23, "cmd23"),
        (24, "cmd24"),
        (30, "cmd30"),
        (31, "cmd31"),
    ]
    for cmd, name in handshake_sequence:
        env = control_envelope(cmd, payload_field=cmd, payload_bytes=b"")
        send_frame(sock, name, env)
        consume(sock, f"after {name}", timeout_s=1.0)

    # Step 4: hold open and watch for button events.
    print("\n=== Now press the Alexa button. Holding for 30 s. ===\n")
    sock.settimeout(0.3)
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            data = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError as e:
            print(f"  recv error: {e}")
            break
        if not data:
            print("  remote closed.")
            break
        print(f"\n[t={30 - (deadline - time.monotonic()):5.2f}s] {len(data)}B")
        print(hexdump(data))
        rest = data
        while rest:
            f, rest = parse_frame(rest)
            if not f:
                break
            if f["streamId"] == 0:
                print("  ControlEnvelope:")
                print(parse_protobuf(f["payload"], depth=2))

    sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
