"""Cross-thread progress bus from action executors → the OSD.

Long-running actions (Spotify search, network calls, multi-step shell
commands) run inside the inject-worker QThread. From there they can call
``emit_progress("Searching for ...")`` to push a narration line that
shows up in the center of the OSD while the leading state label still
says "Executing". Qt auto-queues the signal across thread boundaries
so the slot runs on the GUI thread without explicit invokeMethod
plumbing.

A single module-level QObject owns the Signal so any action import is
free to call ``emit_progress`` without holding a reference to the
MainWindow. The MainWindow connects to ``progress_signal()`` once at
startup.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class _ProgressBus(QObject):
    progress = Signal(str)


_bus = _ProgressBus()


def emit_progress(text: str) -> None:
    """Post a progress narration line to the OSD. Safe from any thread."""
    if not isinstance(text, str):
        return
    _bus.progress.emit(text)


def progress_signal() -> Signal:
    """The Signal a slot should connect to (typically once, at startup)."""
    return _bus.progress
