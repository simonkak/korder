#!/usr/bin/python3
"""probe-ama-scan.py — sweep AMA command numbers to find what's supported.

Sends `ControlEnvelope { command=N, payload<field N>=empty }` for N in 1..127
and prints whichever come back as something other than ErrorCode=UNSUPPORTED.

Used after probe-ama-handshake.py confirmed GET_DEVICE_INFORMATION (cmd=20)
works. We need to find the *other* commands the host needs to send after
GetDeviceInformation to be promoted to "active Alexa host" — without those,
the Alexa button press routes to the device's local "no app" voice prompt.

Empirical contract: this device's accessories_pb_v1 protocol typically
returns one of three things to a request:

  1. Response with a real payload — command is supported AND has data.
  2. Response with error_code=3 (UNSUPPORTED) — known command, refuses empty.
  3. Silence — command not recognized at all.

Outputs (1) and (2) both confirm the command number is real. (3) is uninteresting.

Usage:
    /usr/bin/python3 scripts/probe-ama-scan.py 00:1B:66:E8:90:10
    /usr/bin/python3 scripts/probe-ama-scan.py 00:1B:66:E8:90:10 --start 1 --end 50

CAUTION: some commands (START_SETUP, COMPLETE_SETUP) may have side effects
or cause the device to disconnect. The script runs them anyway — that's what
we need to discover. If the connection drops mid-scan, just re-run; we'll
log the last command that succeeded.
"""
from __future__ import annotations

import argparse
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


def varint(n: int) -> bytes:
    out = bytearray()
    while n > 0x7f:
        out.append((n & 0x7f) | 0x80)
        n >>= 7
    out.append(n & 0x7f)
    return bytes(out)


def tag(field: int, wire_type: int) -> bytes:
    return varint((field << 3) | wire_type)


def control_envelope_request(command: int) -> bytes:
    """ControlEnvelope { command=N, oneof_field<N>=empty }."""
    out = tag(1, 0) + varint(command)             # field 1: command
    out += tag(command, 2) + varint(0)            # field N: length-delimited, empty
    return out


def frame(envelope: bytes) -> bytes:
    if len(envelope) > 0xff:
        raise ValueError("envelope too big for 8-bit length")
    return b"\x00\x00" + bytes([len(envelope)]) + envelope


def parse_frame(buf: bytes) -> tuple[dict, bytes]:
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
    return {
        "version": (b0 >> 4) & 0x0f,
        "streamId": ((b0 & 0x0f) << 1) | ((b1 >> 7) & 0x01),
        "is16Bits": bool(is16),
        "length": length,
        "payload": buf[offset:offset + length],
    }, buf[offset + length:]


def parse_protobuf_iter(buf: bytes):
    pos = 0
    while pos < len(buf):
        t = 0
        shift = 0
        while pos < len(buf):
            b = buf[pos]; pos += 1
            t |= (b & 0x7f) << shift
            if not (b & 0x80):
                break
            shift += 7
        else:
            return
        field = t >> 3
        wt = t & 0x07
        if wt == 0:
            v = 0; shift = 0
            while pos < len(buf):
                b = buf[pos]; pos += 1
                v |= (b & 0x7f) << shift
                if not (b & 0x80):
                    break
                shift += 7
            yield (field, "varint", v)
        elif wt == 2:
            ln = 0; shift = 0
            while pos < len(buf):
                b = buf[pos]; pos += 1
                ln |= (b & 0x7f) << shift
                if not (b & 0x80):
                    break
                shift += 7
            sub = buf[pos:pos + ln]; pos += ln
            yield (field, "msg", sub)
        else:
            return


def classify(envelope: bytes, expected_command: int) -> str:
    """Return a short label for a response to a probe."""
    fields = list(parse_protobuf_iter(envelope))
    if not fields:
        return "EMPTY"
    cmd = next((v for f, t, v in fields if f == 1 and t == "varint"), None)
    response_msg = next((v for f, t, v in fields if t == "msg"), None)

    if cmd is None:
        return f"NO_CMD ({envelope.hex()})"
    if cmd != expected_command:
        return f"WRONG_CMD got={cmd}"
    if response_msg is None:
        return f"NO_PAYLOAD"

    # Unwrap Response. Field 1 = error_code (varint), other fields = payload oneof.
    rfields = list(parse_protobuf_iter(response_msg))
    err = next((v for f, t, v in rfields if f == 1 and t == "varint"), 0)
    payload_fields = [f for f, t, v in rfields if f != 1]

    err_names = {
        0: "SUCCESS", 1: "UNKNOWN", 2: "INTERNAL", 3: "UNSUPPORTED",
        4: "USER_CANCELLED", 5: "NOT_FOUND", 6: "INVALID", 7: "BUSY",
    }

    if err == 3 and not payload_fields:
        return "UNSUPPORTED"
    if err == 0 and payload_fields:
        return f"OK payload-fields={payload_fields}"
    return f"ERR={err_names.get(err, err)} fields={payload_fields}"


def recv_window(sock: socket.socket, max_seconds: float) -> bytes:
    sock.settimeout(0.3)
    chunks = []
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        try:
            d = sock.recv(4096)
        except socket.timeout:
            if chunks:
                break
            continue
        except OSError:
            break
        if not d:
            break
        chunks.append(d)
    return b"".join(chunks)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("mac")
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--end", type=int, default=120)
    p.add_argument("--skip", type=int, nargs="*", default=[20, 28, 102],
                   help="commands already known")
    p.add_argument("--per-cmd-wait", type=float, default=0.6,
                   help="seconds to wait for a response after each send")
    args = p.parse_args(argv[1:])

    print(f"connecting to {args.mac} on RFCOMM ch 19...")
    sock = connect_rfcomm(args.mac, 19)
    print("  connected. discarding greeting.")
    _ = recv_window(sock, 1.5)  # eat greeting

    print(f"\nscanning commands {args.start}..{args.end} (skip {args.skip})\n")
    interesting = []
    for n in range(args.start, args.end + 1):
        if n in args.skip:
            continue
        env = control_envelope_request(n)
        sock.sendall(frame(env))
        data = recv_window(sock, args.per_cmd_wait)
        if not data:
            print(f"  cmd {n:3d}: silence")
            continue

        # Parse frames out of data; classify the first ControlEnvelope.
        rest = data
        labels = []
        while rest:
            f, rest = parse_frame(rest)
            if not f:
                break
            if f["streamId"] == 0:
                labels.append(classify(f["payload"], n))
            else:
                labels.append(f"streamId={f['streamId']} {len(f['payload'])}B")
        label = " | ".join(labels)
        print(f"  cmd {n:3d}: {label}")
        if any(("OK" in l or "ERR" in l) for l in labels):
            interesting.append((n, label, data))

    print("\n=== SUMMARY ===")
    print(f"interesting commands ({len(interesting)}):")
    for n, label, raw in interesting:
        print(f"  {n}: {label}")
        print(f"     raw: {raw.hex()}")

    print("\nholding for 30 s — press Alexa button to test if scan unlocked the device...")
    sock.settimeout(0.3)
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            d = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not d:
            print("remote closed.")
            break
        print(f"\n[t+{30 - (deadline - time.monotonic()):5.2f}s] {len(d)}B: {d.hex()}")
        rest = d
        while rest:
            f, rest = parse_frame(rest)
            if not f or f["streamId"] != 0:
                break
            for field, ttype, val in parse_protobuf_iter(f["payload"]):
                if ttype == "varint":
                    print(f"  field {field} (varint) = {val}")
                else:
                    print(f"  field {field} (msg, {len(val)}B) = {val.hex()}")

    sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
