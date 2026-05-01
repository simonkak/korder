"""AMA RFCOMM button-press listener.

Connects to the Sennheiser PXC 550-II's AMA service over RFCOMM and emits
a Qt signal each time the headphone's Alexa button is pressed. Designed
to run as a single QThread inside the main korder process; on disconnect
it backs off and reconnects automatically.

Protocol notes (reverse-engineered from probe-ama-connect.py traces):

- The RFCOMM channel for AMA on this firmware (modalias
  bluetooth:v0492p600Dd0106) is 19. We try the configured default first,
  then sweep 14..30 if it refuses.
- On connect, the device immediately sends a 20-byte greeting starting
  with the magic `fe 03 01`. We discard it.
- After the greeting, *any* frame the device sends is treated as a button
  event. We do not parse the protobuf payload — for MVP, "frame received
  on this socket" is sufficient signal. Refining gesture detection
  (long-press, double-tap) is a follow-up that needs more captured
  samples.
- A single button press is sometimes split across multiple recv() reads.
  We coalesce consecutive frames within a 200-ms debounce window into one
  emission, so users don't get double-triggered toggles.
"""
from __future__ import annotations

import errno
import os
import socket
import sys
import time
from typing import Sequence

from PySide6.QtCore import QThread, Signal

from . import _rfcomm


def _log(msg: str) -> None:
    """Log AMA listener events to stderr; gated on KORDER_DEBUG_BT=1."""
    if os.environ.get("KORDER_DEBUG_BT", "1") != "0":
        print(f"[korder.ama] {msg}", file=sys.stderr, flush=True)

# Initial sleep between reconnect attempts; doubles up to MAX_BACKOFF_S.
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0

# After this many consecutive ECONNREFUSED across all candidate channels,
# log a "headphones likely not Alexa-registered" hint and keep retrying at
# MAX_BACKOFF_S — but only emit the hint once.
DORMANT_HINT_THRESHOLD = 6

# Coalesce frames arriving within this window into a single button press.
DEBOUNCE_S = 0.2

# When the configured channel is refused, sweep this range looking for
# a different channel that emits an AMA greeting.
FALLBACK_CHANNELS: Sequence[int] = tuple(range(14, 31))

# Bytes the device sends right after connect — discarded, never treated
# as a button press. The exact greeting may vary between firmware
# revisions, so we use a magic-prefix match rather than length match.
GREETING_PREFIX = b"\xfe\x03\x01"


class AmaButtonListener(QThread):
    """Background thread that holds an RFCOMM connection to the AMA service.

    Emits `pressed` (no arguments) for each button event, after debounce.
    Emits `connected` and `disconnected` for UI status updates.
    """

    pressed = Signal()
    connected = Signal()
    disconnected = Signal()

    def __init__(self, mac: str, channel: int = 19, parent=None):
        super().__init__(parent)
        self._mac = mac
        self._default_channel = channel
        self._stopping = False
        self._sock: socket.socket | None = None

    def stop(self) -> None:
        """Ask the listener to shut down. Safe to call from any thread."""
        self._stopping = True
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self.wait(2000)

    def run(self) -> None:
        backoff = INITIAL_BACKOFF_S
        channel = self._default_channel
        consecutive_refusals = 0
        dormant_hint_shown = False
        _log(f"listener starting; target {self._mac} ch {channel}")

        while not self._stopping:
            try:
                _log(f"connecting (ch {channel})...")
                self._sock = _rfcomm.connect_rfcomm(self._mac, channel, timeout_s=1.0)
            except OSError as e:
                # ECONNREFUSED is BlueZ's brief post-disconnect cooldown OR
                # the wrong channel. Try the configured default first, then
                # sweep on persistent refusal.
                if not self._stopping:
                    name = errno.errorcode.get(e.errno or 0, str(e.errno))
                    _log(f"connect failed: {name} ({e.strerror}); retrying in {backoff:.1f}s")
                    if e.errno == errno.ECONNREFUSED:
                        consecutive_refusals += 1
                        if channel == self._default_channel and backoff >= 4.0:
                            channel = self._next_fallback_channel(channel)
                            _log(f"persistent refusal — switching to fallback ch {channel}")
                        if consecutive_refusals == DORMANT_HINT_THRESHOLD and not dormant_hint_shown:
                            dormant_hint_shown = True
                            _log(
                                "headphones refuse RFCOMM on every channel. AMA is likely "
                                "dormant — register the headphones once via the Alexa app on "
                                "a phone, then power-cycle them. See docs/headphone-recon.md."
                            )
                    self._sleep_backoff(backoff)
                    backoff = min(backoff * 2.0, MAX_BACKOFF_S)
                continue
            consecutive_refusals = 0
            dormant_hint_shown = False

            backoff = INITIAL_BACKOFF_S
            channel = self._default_channel
            _log(f"connected on ch {channel}; awaiting frames")
            self.connected.emit()
            try:
                self._read_loop(self._sock)
            finally:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
                _log("disconnected")
                self.disconnected.emit()

            if not self._stopping:
                # Brief pause before reconnecting after a clean drop —
                # BlueZ needs a moment to release the channel server-side.
                self._sleep_backoff(INITIAL_BACKOFF_S)

    def _read_loop(self, sock: socket.socket) -> None:
        """Read frames; emit `pressed` once per button event, after debounce."""
        seen_greeting = False
        last_press_t = 0.0

        while not self._stopping:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as e:
                _log(f"recv error: {e}")
                return
            if not data:
                _log("remote closed connection (recv 0 bytes)")
                return

            if not seen_greeting and data.startswith(GREETING_PREFIX):
                seen_greeting = True
                _log(f"greeting received ({len(data)} bytes); ready for button events")
                continue

            now = time.monotonic()
            # Treat anything else as a button event, but debounce — a single
            # press sometimes arrives split across two recv() calls.
            if now - last_press_t >= DEBOUNCE_S:
                _log(f"button frame ({len(data)} bytes): {data.hex()}")
                self.pressed.emit()
                last_press_t = now
            else:
                _log(f"frame within debounce window — ignored ({len(data)} bytes)")

    def _sleep_backoff(self, seconds: float) -> None:
        """Sleep up to `seconds` while remaining responsive to stop()."""
        end = time.monotonic() + seconds
        while not self._stopping and time.monotonic() < end:
            time.sleep(0.1)

    def _next_fallback_channel(self, current: int) -> int:
        """Pick the next channel to try after persistent refusal on default."""
        for ch in FALLBACK_CHANNELS:
            if ch != current:
                return ch
        return current
