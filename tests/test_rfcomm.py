"""Tests for the AF_BLUETOOTH RFCOMM ctypes shim.

The shim's only pure function is sockaddr_rc packing — exercise edges
on MAC formatting and channel range. Connect itself isn't tested here
(needs hardware).
"""
from __future__ import annotations

import pytest

from korder.triggers._rfcomm import _pack_sockaddr_rc, SOCKADDR_RC_SIZE


def test_pack_size_matches_kernel_expectation():
    addr = _pack_sockaddr_rc("00:11:22:33:44:55", 19)
    assert len(addr) == SOCKADDR_RC_SIZE == 10


def test_pack_family_bytes_first():
    # AF_BLUETOOTH = 31 little-endian: 0x1f 0x00
    addr = _pack_sockaddr_rc("00:11:22:33:44:55", 1)
    assert addr[0:2] == b"\x1f\x00"


def test_pack_bdaddr_is_reversed():
    # The bdaddr field is little-endian byte order — the ASCII MAC
    # AA:BB:CC:DD:EE:FF must serialize as FF EE DD CC BB AA in the struct.
    addr = _pack_sockaddr_rc("AA:BB:CC:DD:EE:FF", 1)
    assert addr[2:8] == b"\xff\xee\xdd\xcc\xbb\xaa"


def test_pack_channel_byte():
    addr = _pack_sockaddr_rc("00:11:22:33:44:55", 19)
    assert addr[8] == 19


def test_pack_real_pxc550_mac():
    # The MAC the user's headphones present in recon traces.
    addr = _pack_sockaddr_rc("00:1B:66:E8:90:10", 19)
    expected = bytes.fromhex("1f00") + bytes.fromhex("1090E8661B00") + bytes([19, 0])
    assert addr == expected


def test_pack_rejects_invalid_channel_low():
    with pytest.raises(ValueError):
        _pack_sockaddr_rc("00:11:22:33:44:55", 0)


def test_pack_rejects_invalid_channel_high():
    with pytest.raises(ValueError):
        _pack_sockaddr_rc("00:11:22:33:44:55", 31)


def test_pack_rejects_short_mac():
    with pytest.raises(ValueError):
        _pack_sockaddr_rc("00:11:22", 1)


def test_pack_case_insensitive_mac():
    a = _pack_sockaddr_rc("aa:bb:cc:dd:ee:ff", 5)
    b = _pack_sockaddr_rc("AA:BB:CC:DD:EE:FF", 5)
    assert a == b
