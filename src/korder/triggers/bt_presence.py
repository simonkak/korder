"""Watch BlueZ for connect/disconnect of a specific Bluetooth device.

Uses `gdbus monitor` as a subprocess rather than a Python DBus binding.
Why: zero new Python deps (gdbus ships with glib2 on every Linux KDE
install); the parser is tiny because we only care about a single
property; and a hung gdbus is trivially recoverable by killing the
subprocess, whereas a hung Python DBus loop is harder to clean up.

The watcher emits two Qt signals:
- `connected` — when Device1.Connected transitions from False to True
- `disconnected` — when it transitions True → False

It also emits the initial state on startup so consumers can prime
themselves regardless of which order the headphones and korder come up.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

from PySide6.QtCore import QThread, Signal


def _log(msg: str) -> None:
    if os.environ.get("KORDER_DEBUG_BT", "1") != "0":
        print(f"[korder.bt] {msg}", file=sys.stderr, flush=True)


def _device_path(mac: str) -> str:
    return f"/org/bluez/hci0/dev_{mac.replace(':', '_')}"


def _query_connected(mac: str) -> bool | None:
    """One-shot: ask BlueZ for the device's current Connected property.

    Returns True/False on success, None if the call failed (device unknown,
    BlueZ down, …) — caller treats that as "not connected".
    """
    cmd = [
        "gdbus", "call", "--system",
        "--dest", "org.bluez",
        "--object-path", _device_path(mac),
        "--method", "org.freedesktop.DBus.Properties.Get",
        "org.bluez.Device1", "Connected",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    # Output format: "(<true>,)" or "(<false>,)"
    return "true" in out.stdout.lower()


# Matches the relevant fragment of a gdbus PropertiesChanged dump:
#   ... org.bluez.Device1 ... 'Connected': <true> ...
# (Connected may not be the only key in the dict — we extract just it.)
_CONNECTED_RE = re.compile(r"'Connected':\s*<(true|false)>", re.IGNORECASE)


class BluezPresenceWatcher(QThread):
    """Emits `connected` / `disconnected` for a configured BT MAC."""

    connected = Signal()
    disconnected = Signal()

    def __init__(self, mac: str, parent=None):
        super().__init__(parent)
        self._mac = mac
        self._stopping = False
        self._proc: subprocess.Popen | None = None
        self._last_state: bool | None = None

    def stop(self) -> None:
        self._stopping = True
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
        self.wait(2000)

    def run(self) -> None:
        # Emit initial state so the rest of the app can configure itself.
        initial = _query_connected(self._mac)
        if initial is not None:
            self._update(initial)

        while not self._stopping:
            try:
                self._monitor_once()
            except FileNotFoundError:
                # gdbus missing — fail quietly, this is an optional feature.
                return
            if not self._stopping:
                # gdbus exited (e.g. dbus restart). Re-check current state
                # so we don't miss a transition that happened during the gap.
                state = _query_connected(self._mac)
                if state is not None:
                    self._update(state)
                time.sleep(2.0)

    def _monitor_once(self) -> None:
        cmd = [
            "gdbus", "monitor", "--system",
            "--dest", "org.bluez",
            "--object-path", _device_path(self._mac),
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,  # line-buffered
        )
        try:
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                if self._stopping:
                    break
                m = _CONNECTED_RE.search(line)
                if m:
                    self._update(m.group(1).lower() == "true")
        finally:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self._proc.kill()
                except OSError:
                    pass
            self._proc = None

    def _update(self, is_connected: bool) -> None:
        if is_connected == self._last_state:
            return
        _log(f"state: {'CONNECTED' if is_connected else 'DISCONNECTED'} ({self._mac})")
        self._last_state = is_connected
        if is_connected:
            self.connected.emit()
        else:
            self.disconnected.emit()
