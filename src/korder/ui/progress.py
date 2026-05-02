"""Cross-thread progress bus from action executors → the OSD.

Long-running actions (Spotify search, network calls, multi-step shell
commands) run inside the inject-worker QThread. From there they can call
``emit_progress("Searching for ...")`` to push a narration line that
shows up in the center of the OSD while the leading state label still
says "Executing". Qt auto-queues the signal across thread boundaries
so the slot runs on the GUI thread without explicit invokeMethod
plumbing.

Two signals on the same bus:
  - progress (str)               — OSD narration only; always emitted
  - progress_speak (str, str)    — narration that should ALSO be spoken
                                   via TTS when [tts] enabled. Second
                                   arg is a lang code ('pl' / 'en' /
                                   'auto'). Use sparingly — most
                                   progress lines are visual feedback,
                                   not chatter the user wants voiced.

A single module-level QObject owns the Signals so any action import is
free to call ``emit_progress`` without holding a reference to the
MainWindow. The MainWindow connects to both signals once at startup.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class _ProgressBus(QObject):
    progress = Signal(str)
    progress_speak = Signal(str, str)


_bus = _ProgressBus()


def emit_progress(text: str) -> None:
    """Post a progress narration line to the OSD. Safe from any thread."""
    if not isinstance(text, str):
        return
    _bus.progress.emit(text)


def emit_progress_speak(text: str, lang: str = "auto") -> None:
    """Post a progress line that should ALSO be spoken via TTS (when
    enabled in config). Routes through the same OSD path AND the speak
    signal — caller doesn't have to emit twice. Lang code is one of
    'pl', 'en', or 'auto'."""
    if not isinstance(text, str) or not text:
        return
    _bus.progress.emit(text)
    _bus.progress_speak.emit(text, lang or "auto")


def progress_signal() -> Signal:
    """The OSD-only Signal a slot should connect to (typically once, at startup)."""
    return _bus.progress


def progress_speak_signal() -> Signal:
    """The TTS-routing Signal. Slot signature: (text: str, lang: str)."""
    return _bus.progress_speak
