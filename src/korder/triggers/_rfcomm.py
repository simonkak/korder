"""ctypes shim for Bluetooth RFCOMM connect on Pythons missing AF_BLUETOOTH.

The uv-managed Python (python-build-standalone) is compiled in an
environment that lacks <bluetooth/bluetooth.h>, so its socket module
won't pack `sockaddr_rc` for a `connect()` call — it raises
"connect(): bad family". The kernel itself accepts AF_BLUETOOTH=31
syscalls fine; we just need to bypass Python's address translation.

This module opens a stdlib socket via `socket.socket(31, SOCK_STREAM, 3)`
(numeric constants — those work on any Python build), then calls libc's
`connect()` directly with a manually-packed sockaddr_rc. After connect,
all subsequent operations (recv, close, fileno, settimeout, …) are
plain Python socket API, no shim needed.

Linux only.
"""
from __future__ import annotations

import ctypes
import os
import socket
import struct

AF_BLUETOOTH = 31
BTPROTO_RFCOMM = 3
SOCKADDR_RC_SIZE = 10  # 2-byte family + 6-byte bdaddr + 1-byte channel + 1 padding

_libc = ctypes.CDLL("libc.so.6", use_errno=True)


def _pack_sockaddr_rc(mac: str, channel: int) -> bytes:
    """Pack a sockaddr_rc for the Linux Bluetooth RFCOMM family.

    The bdaddr field is little-endian — the byte order of the MAC string
    (`AA:BB:CC:DD:EE:FF`) must be reversed before packing.
    """
    if channel < 1 or channel > 30:
        raise ValueError(f"RFCOMM channel must be 1..30, got {channel}")
    raw = bytes.fromhex(mac.replace(":", ""))
    if len(raw) != 6:
        raise ValueError(f"bad MAC: {mac!r}")
    bdaddr_le = raw[::-1]
    body = struct.pack("<H6sB", AF_BLUETOOTH, bdaddr_le, channel)
    return body + b"\x00" * (SOCKADDR_RC_SIZE - len(body))


def connect_rfcomm(mac: str, channel: int, *, timeout_s: float | None = None) -> socket.socket:
    """Open + connect an RFCOMM socket. Returns a normal Python socket.

    Sets the socket to blocking mode after connect (or applies the requested
    timeout if `timeout_s` is given). The caller is responsible for
    closing the socket.

    Raises OSError on connect failure, with errno set as expected
    (ECONNREFUSED, EHOSTUNREACH, ETIMEDOUT, EBUSY, …).
    """
    s = socket.socket(AF_BLUETOOTH, socket.SOCK_STREAM, BTPROTO_RFCOMM)
    addr = _pack_sockaddr_rc(mac, channel)

    # NOTE: we don't apply timeout via Python's settimeout before connect,
    # because that path also goes through the broken address packing.
    # Instead we connect blockingly via libc, then optionally settimeout
    # for subsequent recv/send.
    ret = _libc.connect(s.fileno(), addr, SOCKADDR_RC_SIZE)
    if ret != 0:
        err = ctypes.get_errno()
        try:
            s.close()
        except OSError:
            pass
        raise OSError(err, os.strerror(err), f"connect({mac}, ch={channel})")

    if timeout_s is not None:
        s.settimeout(timeout_s)
    return s
