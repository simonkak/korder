#!/usr/bin/python3
"""parse-btsnoop.py — extract RFCOMM frames from an Android BT snoop dump.

The Android `dumpsys bluetooth_manager` output contains an inline
BTSNOOP_LOG_SUMMARY section: the first ~9 bytes are a custom header,
followed by zlib-compressed btsnoop binary log.

This script:

  1. Reads dumpsys output.
  2. Extracts the base64 block between BEGIN:/END:BTSNOOP_LOG_SUMMARY.
  3. Base64-decodes it, then zlib-decompresses (auto-detecting the
     offset where the zlib stream starts).
  4. Parses the resulting btsnoop file (standard format, big-endian).
  5. Filters to RFCOMM frames on the configured channel.
  6. Reassembles each RFCOMM PDU and prints AMA control envelopes
     in a human-readable form.

Usage:
    /usr/bin/python3 scripts/parse-btsnoop.py ~/bt-dump.txt
    /usr/bin/python3 scripts/parse-btsnoop.py ~/bt-dump.txt --channel 19 --bdaddr 00:1B:66:E8:90:10
    /usr/bin/python3 scripts/parse-btsnoop.py --raw-bin /tmp/btsnoop.log  # already-extracted .log
"""
from __future__ import annotations

import argparse
import base64
import re
import struct
import sys
import zlib


# ---------- Step 1: extract base64 + decompress ----------

def extract_compressed_blob(dumpsys_path: str) -> bytes:
    with open(dumpsys_path) as f:
        text = f.read()
    m = re.search(
        r"-+\s*BEGIN:BTSNOOP_LOG_SUMMARY\b.*?-+\s*\n(.*?)\n\s*-+\s*END:BTSNOOP_LOG_SUMMARY",
        text, re.DOTALL,
    )
    if not m:
        raise RuntimeError("no BTSNOOP_LOG_SUMMARY block found")
    block = m.group(1)
    # Concatenate all base64 lines (strip whitespace, skip blank lines).
    b64 = "".join(line.strip() for line in block.splitlines() if line.strip())
    raw = base64.b64decode(b64)
    return raw


def decompress_snoop(raw: bytes) -> bytes:
    """Find the zlib stream in `raw` and decompress.

    Heytap/Oppo/AOSP wrap the snoop log with a small header before the
    zlib stream. We probe for the zlib magic byte 0x78 in the first 32
    bytes and decompress from there.
    """
    # Try every plausible offset for the zlib stream start. zlib magic
    # byte is 0x78; the next byte is 0x01/0x5E/0x9C/0xDA depending on
    # compression level.
    for start in range(0, 32):
        if start + 1 >= len(raw):
            break
        if raw[start] == 0x78 and raw[start + 1] in (0x01, 0x5E, 0x9C, 0xDA):
            try:
                return zlib.decompress(raw[start:])
            except zlib.error:
                continue
    # Fallback: try raw deflate (some implementations skip the zlib wrapper).
    for start in range(0, 32):
        try:
            return zlib.decompress(raw[start:], -zlib.MAX_WBITS)
        except zlib.error:
            continue
    raise RuntimeError("no decompressible zlib stream found in extracted blob")


# ---------- Step 2: parse btsnoop file format ----------

# btsnoop file header (16 bytes): magic "btsnoop\0" + uint32 version + uint32 datalink
BTSNOOP_MAGIC = b"btsnoop\x00"


def iter_btsnoop_packets(snoop: bytes):
    """Yield (direction_in:bool, ts_us:int, payload:bytes) for each packet."""
    if snoop[:8] != BTSNOOP_MAGIC:
        # Some Android variants use a slightly different magic / header
        # — skip up to 64 bytes looking for it.
        idx = snoop.find(BTSNOOP_MAGIC, 0, 256)
        if idx == -1:
            raise RuntimeError(f"btsnoop magic not found; first 32 bytes: {snoop[:32].hex()}")
        snoop = snoop[idx:]

    pos = 16  # skip file header
    while pos + 24 <= len(snoop):
        # packet record: original_len, included_len, flags, drops, ts_us (BE)
        orig_len, incl_len, flags, _drops, ts_high, ts_low = struct.unpack(
            ">IIIIII", snoop[pos:pos + 24]
        )
        pos += 24
        if pos + incl_len > len(snoop):
            break
        payload = snoop[pos:pos + incl_len]
        pos += incl_len

        # flags bit 0: 1 = direction host->controller (received from host), 0 = controller->host
        # flags bit 1: 1 = command/event, 0 = data
        # In btsnoop, "direction" semantics: bit 0 of flags = 1 means "received" by host
        # i.e. 0 = HOST -> CONTROLLER (sent), 1 = CONTROLLER -> HOST (received)
        is_received = bool(flags & 0x01)
        ts_us = (ts_high << 32) | ts_low
        yield is_received, ts_us, payload


# ---------- Step 3: HCI ACL → L2CAP → RFCOMM reassembly ----------

# We track two flows: outbound (host->ctrl) and inbound (ctrl->host) per
# (handle, channel). Each gets a reassembly buffer.

class L2capReassembler:
    def __init__(self):
        # key = (acl_handle, l2cap_cid)
        self._buf: dict[tuple[int, int], tuple[int, bytearray]] = {}

    def feed(self, handle: int, pb_flag: int, payload: bytes):
        """pb_flag 0b10 = first fragment, 0b01 = continuation."""
        if pb_flag == 0b10:  # first fragment
            if len(payload) < 4:
                return None
            l2cap_len, cid = struct.unpack("<HH", payload[:4])
            buf = bytearray(payload[4:])
            if len(buf) >= l2cap_len:
                return cid, bytes(buf[:l2cap_len])
            self._buf[(handle, cid)] = (l2cap_len, buf)
            return None
        elif pb_flag == 0b01:  # continuation
            # Find the active reassembly for this handle (cid is unknown until first frag)
            for (h, c), (need, buf) in list(self._buf.items()):
                if h == handle:
                    buf.extend(payload)
                    if len(buf) >= need:
                        del self._buf[(h, c)]
                        return c, bytes(buf[:need])
                    return None
            return None
        else:
            return None


def parse_rfcomm_pdu(pdu: bytes) -> dict | None:
    """Decode an RFCOMM frame (TS 07.10 / GSM with 1-byte FCS).

    RFCOMM addr byte:    EA(1) | CR(1) | DLCI(6)
    DLCI = (channel << 1) | direction. So channel = (dlci >> 1) & 0x1f.
    """
    if len(pdu) < 4:
        return None
    addr = pdu[0]
    control = pdu[1]
    # Length: 1 byte (EA=1) or 2 bytes (EA=0)
    if pdu[2] & 0x01:  # EA bit
        length = pdu[2] >> 1
        info_start = 3
    else:
        length = ((pdu[3] << 7) | (pdu[2] >> 1))
        info_start = 4
    # FCS is the last byte (we don't validate)
    info = pdu[info_start:info_start + length]

    dlci = (addr >> 2) & 0x3f
    channel = dlci >> 1
    # Skip non-UIH frames (control frames don't carry user data).
    is_uih = (control & 0xef) == 0xef  # UIH with or without P/F
    return {
        "channel": channel,
        "control": control,
        "is_uih": is_uih,
        "info": info,
    }


# ---------- Step 4: AMA frame parsing ----------

def parse_ama_frame(buf: bytes) -> tuple[dict, bytes] | None:
    """Parse one AMA transport frame from `buf`. Returns (frame, rest)."""
    if len(buf) < 3:
        return None
    b0, b1 = buf[0], buf[1]
    is16 = b1 & 0x01
    if is16:
        if len(buf) < 4:
            return None
        length = (buf[2] << 8) | buf[3]
        offset = 4
    else:
        length = buf[2]
        offset = 3
    if len(buf) < offset + length:
        return None
    return {
        "version": (b0 >> 4) & 0x0f,
        "streamId": ((b0 & 0x0f) << 1) | ((b1 >> 7) & 0x01),
        "is16": bool(is16),
        "length": length,
        "payload": buf[offset:offset + length],
    }, buf[offset + length:]


def parse_protobuf(buf: bytes, depth: int = 0) -> str:
    indent = "  " * depth
    out = []
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
            return "\n".join(out)
        field = t >> 3; wt = t & 0x07
        if wt == 0:
            v = 0; shift = 0
            while pos < len(buf):
                b = buf[pos]; pos += 1
                v |= (b & 0x7f) << shift
                if not (b & 0x80):
                    break
                shift += 7
            out.append(f"{indent}f{field} (varint) = {v}")
        elif wt == 2:
            ln = 0; shift = 0
            while pos < len(buf):
                b = buf[pos]; pos += 1
                ln |= (b & 0x7f) << shift
                if not (b & 0x80):
                    break
                shift += 7
            sub = buf[pos:pos + ln]; pos += ln
            if sub and all(0x20 <= c < 0x7f for c in sub):
                out.append(f"{indent}f{field} (string, {ln}B) = {sub.decode()!r}")
            else:
                out.append(f"{indent}f{field} (msg, {ln}B) {{")
                if sub:
                    out.append(parse_protobuf(sub, depth + 1))
                out.append(f"{indent}}}")
        else:
            out.append(f"{indent}f{field} (wt={wt}) <skipped>")
            return "\n".join(out)
    return "\n".join(out)


# ---------- Step 5: pipeline ----------

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("input", help="Path to dumpsys output file (or a raw .log if --raw-bin)")
    p.add_argument("--raw-bin", action="store_true",
                   help="Input is already a decompressed btsnoop binary file")
    p.add_argument("--channel", type=int, default=19,
                   help="RFCOMM channel to filter (default 19 = AMA)")
    p.add_argument("--save-bin", default="",
                   help="If set, save the decompressed btsnoop binary to this path")
    p.add_argument("--show-all-channels", action="store_true",
                   help="Don't filter; print every RFCOMM channel observed")
    args = p.parse_args(argv[1:])

    # Acquire raw btsnoop bytes.
    if args.raw_bin:
        with open(args.input, "rb") as f:
            snoop = f.read()
    else:
        print(f"reading {args.input}")
        raw = extract_compressed_blob(args.input)
        print(f"  base64-decoded: {len(raw)} bytes")
        print(f"  first 16 bytes: {raw[:16].hex()}")
        snoop = decompress_snoop(raw)
        print(f"  decompressed:   {len(snoop)} bytes")
        print(f"  first 16 bytes: {snoop[:16].hex()}")

    if args.save_bin:
        with open(args.save_bin, "wb") as f:
            f.write(snoop)
        print(f"  saved decompressed btsnoop -> {args.save_bin}")

    # Walk packets, reassemble RFCOMM, find AMA frames.
    reass = L2capReassembler()
    target_channels: set[int] | None
    if args.show_all_channels:
        target_channels = None
    else:
        target_channels = {args.channel}

    rfcomm_partial: dict[tuple[bool, int], bytearray] = {}  # per (direction, channel)
    pkt_count = 0
    rfcomm_count = 0
    ama_count = 0

    print(f"\n=== walking btsnoop packets, looking for RFCOMM "
          f"{'all channels' if target_channels is None else 'ch ' + str(args.channel)} ===\n")

    for is_received, ts_us, payload in iter_btsnoop_packets(snoop):
        pkt_count += 1
        # First byte is HCI packet type: 0x02 = ACL data, 0x01 = command,
        # 0x04 = event, etc. We only care about ACL.
        if not payload:
            continue
        hci_type = payload[0]
        if hci_type != 0x02:
            continue
        if len(payload) < 5:
            continue
        # ACL header (LE): handle+flags(2), data_total_len(2)
        h_and_flags, total_len = struct.unpack("<HH", payload[1:5])
        handle = h_and_flags & 0x0fff
        pb_flag = (h_and_flags >> 12) & 0x03
        acl_data = payload[5:5 + total_len]
        result = reass.feed(handle, pb_flag, acl_data)
        if result is None:
            continue
        cid, l2cap_payload = result
        # AMA RFCOMM uses dynamic L2CAP CIDs. Try parsing as RFCOMM regardless.
        rf = parse_rfcomm_pdu(l2cap_payload)
        if rf is None or not rf["is_uih"] or not rf["info"]:
            continue
        rfcomm_count += 1
        ch = rf["channel"]
        if target_channels is not None and ch not in target_channels:
            continue

        # Append to per-direction-per-channel buffer; AMA frames may
        # cross multiple RFCOMM PDUs.
        key = (is_received, ch)
        buf = rfcomm_partial.setdefault(key, bytearray())
        buf.extend(rf["info"])

        # Try to parse complete AMA frames.
        while True:
            res = parse_ama_frame(bytes(buf))
            if res is None:
                break
            frame, rest = res
            buf.clear(); buf.extend(rest)
            ama_count += 1
            direction = "DEV→PHONE" if is_received else "PHONE→DEV"
            ts_s = ts_us / 1_000_000
            print(f"[t={ts_s:14.6f}s] [{direction}] [ch{ch}] "
                  f"streamId={frame['streamId']} length={frame['length']}")
            print(f"  raw envelope: {frame['payload'].hex()}")
            if frame["streamId"] == 0:
                print("  ControlEnvelope:")
                print(parse_protobuf(frame["payload"], depth=2))
            print()

    print(f"\n=== summary ===")
    print(f"  total HCI packets: {pkt_count}")
    print(f"  RFCOMM UIH frames: {rfcomm_count}")
    print(f"  AMA control envelopes: {ama_count}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
