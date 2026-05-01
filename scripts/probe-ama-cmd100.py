#!/usr/bin/python3
"""probe-ama-cmd100.py — try populated payloads for AMA cmd 100 (auth/setup).

Empty payload returns INVALID with empty field 7 (no schema hint). This
script sends a few candidate populated payloads to see if any:

  - Returns a different error_code (e.g. NOT_FOUND, INVALID with details)
    that hints at what's missing.
  - Returns SUCCESS — meaning we accidentally hit a valid input shape.
  - Triggers a state change in the device (voice prompt changes when
    you press the Alexa button afterward).

After each payload attempt the script holds for ~5 seconds; you press
the Alexa button during one of these holds (or all of them) and we
watch for an inbound cmd 102 (ButtonNotification) frame.

Run (PXC 550-II MAC):
    /usr/bin/python3 scripts/probe-ama-cmd100.py 00:1B:66:E8:90:10
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
    addr = struct.pack("<H6sB", AF_BLUETOOTH, bdaddr_le, channel) + b"\x00"
    if _libc.connect(s.fileno(), addr, SOCKADDR_RC_SIZE) != 0:
        err = ctypes.get_errno()
        s.close()
        raise OSError(err, os.strerror(err))
    return s


# ---------- protobuf hand-encoders ----------

def varint(n: int) -> bytes:
    out = bytearray()
    while n > 0x7f:
        out.append((n & 0x7f) | 0x80); n >>= 7
    out.append(n & 0x7f)
    return bytes(out)


def tag(field: int, wt: int) -> bytes:
    return varint((field << 3) | wt)


def field_varint(field: int, value: int) -> bytes:
    return tag(field, 0) + varint(value)


def field_string(field: int, s: str) -> bytes:
    enc = s.encode()
    return tag(field, 2) + varint(len(enc)) + enc


def field_message(field: int, payload: bytes) -> bytes:
    return tag(field, 2) + varint(len(payload)) + payload


def control_envelope(command: int, payload_field: int, payload: bytes) -> bytes:
    return field_varint(1, command) + field_message(payload_field, payload)


def transport_frame(envelope: bytes) -> bytes:
    if len(envelope) > 0xff:
        raise ValueError("payload too big for 8-bit length")
    return b"\x00\x00" + bytes([len(envelope)]) + envelope


# ---------- frame parser ----------

def parse_frames(buf: bytes):
    out = []
    pos = 0
    while pos + 3 <= len(buf):
        b0, b1 = buf[pos], buf[pos + 1]
        is16 = b1 & 0x01
        if is16:
            if pos + 4 > len(buf):
                break
            length = (buf[pos + 2] << 8) | buf[pos + 3]
            offset = pos + 4
        else:
            length = buf[pos + 2]
            offset = pos + 3
        if offset + length > len(buf):
            break
        out.append({
            "streamId": ((b0 & 0x0f) << 1) | ((b1 >> 7) & 0x01),
            "is16": bool(is16),
            "length": length,
            "payload": buf[offset:offset + length],
        })
        pos = offset + length
    return out


def parse_protobuf_iter(buf: bytes):
    pos = 0
    while pos < len(buf):
        t = 0; shift = 0
        while pos < len(buf):
            b = buf[pos]; pos += 1
            t |= (b & 0x7f) << shift
            if not (b & 0x80):
                break
            shift += 7
        else:
            return
        field = t >> 3; wt = t & 7
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


def hexdump(data: bytes, indent: str = "    ") -> str:
    rows = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        h = " ".join(f"{b:02x}" for b in chunk)
        a = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        rows.append(f"{indent}{off:04x}  {h:<47}  {a}")
    return "\n".join(rows)


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


def describe_response(frames: list) -> str:
    descs = []
    for f in frames:
        if f["streamId"] != 0:
            descs.append(f"stream{f['streamId']} {len(f['payload'])}B")
            continue
        cmd = None
        response = None
        unsolicited_field = None
        for field, wt, val in parse_protobuf_iter(f["payload"]):
            if field == 1 and wt == "varint":
                cmd = val
            elif wt == "msg":
                if field == 9:
                    response = val
                else:
                    unsolicited_field = (field, val)
        parts = []
        if cmd is not None:
            parts.append(f"cmd={cmd}")
        if response is not None:
            err = 0
            payload_fields = []
            for rf, rwt, rv in parse_protobuf_iter(response):
                if rf == 1 and rwt == "varint":
                    err = rv
                else:
                    payload_fields.append((rf, rwt, rv))
            err_names = {
                0: "SUCCESS", 1: "UNKNOWN", 2: "INTERNAL", 3: "UNSUPPORTED",
                4: "USER_CANCELLED", 5: "NOT_FOUND", 6: "INVALID", 7: "BUSY",
            }
            parts.append(f"err={err_names.get(err, err)}")
            if payload_fields:
                pf_desc = []
                for rf, rwt, rv in payload_fields:
                    if rwt == "varint":
                        pf_desc.append(f"f{rf}={rv}")
                    else:
                        pf_desc.append(f"f{rf}={len(rv)}B:{rv.hex()}")
                parts.append(f"payload[{','.join(pf_desc)}]")
        if unsolicited_field is not None:
            uf_field, uf_val = unsolicited_field
            parts.append(f"NOTIF f{uf_field} {len(uf_val)}B")
        descs.append("Response{" + " ".join(parts) + "}")
    return " | ".join(descs)


def try_payload(sock: socket.socket, label: str, payload: bytes) -> None:
    env = control_envelope(100, 100, payload)
    frame = transport_frame(env)
    print(f"\n[{label}]")
    print(f"  send envelope: {env.hex()}  ({len(payload)}B payload)")
    print(f"  send frame:    {frame.hex()}")
    sock.sendall(frame)
    data = recv_window(sock, 1.5)
    if not data:
        print("  RECV: (silence)")
        return
    print(f"  RECV ({len(data)}B):")
    print(hexdump(data, indent="    "))
    frames = parse_frames(data)
    print(f"  decoded: {describe_response(frames)}")


def hold_for_button(sock: socket.socket, seconds: float) -> bool:
    print(f"\n  >>> press Alexa button now ({seconds:.0f} s window) <<<")
    sock.settimeout(0.3)
    deadline = time.monotonic() + seconds
    saw_button = False
    while time.monotonic() < deadline:
        try:
            d = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not d:
            break
        for f in parse_frames(d):
            if f["streamId"] == 0:
                fields = list(parse_protobuf_iter(f["payload"]))
                cmd = next((v for fld, t, v in fields if fld == 1 and t == "varint"), None)
                if cmd == 102:
                    print(f"  *** cmd 102 BUTTON FRAME *** {d.hex()}")
                    saw_button = True
                else:
                    print(f"  spontaneous cmd {cmd}: {d.hex()}")
    return saw_button


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__); return 2
    mac = argv[1]

    print(f"connecting to {mac} on RFCOMM ch 19...")
    sock = connect_rfcomm(mac, 19)
    print("  connected. discarding greeting.")
    _ = recv_window(sock, 1.5)

    # Warm up: do the standard handshake first so the device is in
    # "host present" state before we attack cmd 100.
    print("\n=== handshake warm-up ===")
    for cmd in [20, 21]:
        try_payload_at_cmd(sock, cmd, b"")

    # Candidate payloads for cmd 100. f7 in the INVALID response *echoes
    # our parsed input* — wrong-typed payloads get silence (parser
    # bailed); well-typed payloads come back so we can see what was seen.
    #
    # IMPORTANT: do not press the Alexa button during the run — the
    # device's "haven't connected to Alexa app" voice prompt sets up
    # HFP/SCO, which collides with our RFCOMM channel and drops the
    # connection. Hold all button presses until the final 30 s window.
    candidates: list[tuple[str, bytes]] = [
        ("empty (control)", b""),

        # Single varint at f1, sweeping values
        ("f1=0", field_varint(1, 0)),
        ("f1=1", field_varint(1, 1)),
        ("f1=2", field_varint(1, 2)),
        ("f1=100", field_varint(1, 100)),
        ("f1=large (1<<30)", field_varint(1, 1 << 30)),

        # Single varint at other fields
        ("f2=1", field_varint(2, 1)),
        ("f3=1", field_varint(3, 1)),

        # Multi-field varint combos
        ("f1=0,f2=0,f3=0", field_varint(1, 0) + field_varint(2, 0) + field_varint(3, 0)),
        ("f1=1,f2=1", field_varint(1, 1) + field_varint(2, 1)),

        # Bytes (wire type 2) at non-f1 fields (f1=string crashed last run,
        # but let's verify f2 strings are accepted for the f7 echo).
        ("f2=string 'korder'", field_string(2, "korder")),
        ("f2=empty bytes", field_message(2, b"")),

        # Nested empty submsg at various fields
        ("nested empty at f1 (msg, not varint)", field_message(1, b"")),
        ("nested empty at f4", field_message(4, b"")),

        # Mirror DeviceInformation but only varint+message fields, no f1 string
        ("HostInfo-shaped (no f1 string)",
         field_message(2, b"korder".hex().encode())  # safe non-empty bytes
         + field_varint(3, 1)),
    ]

    print("\n=== candidate cmd 100 payloads (no button presses!) ===")
    for label, payload in candidates:
        try:
            try_payload(sock, label, payload)
        except OSError as e:
            print(f"  ! socket dropped during '{label}': {e}")
            print("  reconnecting + re-doing handshake...")
            time.sleep(2.0)
            try:
                sock.close()
            except OSError:
                pass
            sock = connect_rfcomm(mac, 19)
            recv_window(sock, 1.5)  # eat greeting
            for cmd in [20, 21]:
                try_payload_at_cmd(sock, cmd, b"")

    print("\n=== final 30 s button watch — press Alexa button now ===")
    hold_for_button(sock, 30.0)

    sock.close()
    return 0


def try_payload_at_cmd(sock: socket.socket, cmd: int, payload: bytes) -> None:
    env = control_envelope(cmd, cmd, payload)
    sock.sendall(transport_frame(env))
    data = recv_window(sock, 1.0)
    print(f"  cmd {cmd}: " + (describe_response(parse_frames(data)) if data else "silence"))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
