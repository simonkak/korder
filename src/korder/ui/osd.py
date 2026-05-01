"""Multi-state OSD — exposes a state-machine API the rest of the app
drives explicitly. States:

  set_listening()   — mic open, no speech yet (placeholder + cursor)
  set_partial(text) — live transcript streaming
  set_thinking(...) — LLM is reasoning (status hint + faded color)
  set_executing(..) — action firing
  set_pending(...)  — pending parameterized action awaiting user input
  set_committed(..) — committed phrase, optional auto-hide
  hide_now / hide_after — fade out

Each state maps to (prompt, status, showCursor, placeholderMode) on
_OSDState; the QML scene binds against those four properties.
"""
from __future__ import annotations
import os
from pathlib import Path
from PySide6.QtCore import QCoreApplication, QObject, Property, QTimer, Signal
from PySide6.QtQml import QQmlApplicationEngine

from korder.ui.i18n import t

_QML_PATH = Path(__file__).parent / "qml" / "osd.qml"


class _OSDState(QObject):
    """Plain-data QObject exposed to the QML scene as `osdState`."""

    promptChanged = Signal()
    fluxChanged = Signal()
    statusChanged = Signal()
    visibleChanged = Signal()
    showCursorChanged = Signal()
    placeholderModeChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._prompt = ""
        self._flux = ""
        self._status = ""
        self._visible = False
        self._show_cursor = False
        self._placeholder_mode = False

    def _get_prompt(self) -> str:
        return self._prompt

    def _set_prompt(self, value: str) -> None:
        if self._prompt != value:
            self._prompt = value
            self.promptChanged.emit()

    prompt = Property(str, _get_prompt, _set_prompt, notify=promptChanged)

    def _get_flux(self) -> str:
        return self._flux

    def _set_flux(self, value: str) -> None:
        if self._flux != value:
            self._flux = value
            self.fluxChanged.emit()

    flux = Property(str, _get_flux, _set_flux, notify=fluxChanged)

    def _get_status(self) -> str:
        return self._status

    def _set_status(self, value: str) -> None:
        if self._status != value:
            self._status = value
            self.statusChanged.emit()

    status = Property(str, _get_status, _set_status, notify=statusChanged)

    def _get_visible(self) -> bool:
        return self._visible

    def _set_visible(self, value: bool) -> None:
        if self._visible != value:
            self._visible = value
            self.visibleChanged.emit()

    visible = Property(bool, _get_visible, _set_visible, notify=visibleChanged)

    def _get_show_cursor(self) -> bool:
        return self._show_cursor

    def _set_show_cursor(self, value: bool) -> None:
        if self._show_cursor != value:
            self._show_cursor = value
            self.showCursorChanged.emit()

    showCursor = Property(bool, _get_show_cursor, _set_show_cursor, notify=showCursorChanged)

    def _get_placeholder_mode(self) -> bool:
        return self._placeholder_mode

    def _set_placeholder_mode(self, value: bool) -> None:
        if self._placeholder_mode != value:
            self._placeholder_mode = value
            self.placeholderModeChanged.emit()

    placeholderMode = Property(bool, _get_placeholder_mode, _set_placeholder_mode, notify=placeholderModeChanged)


class OSDWindow(QObject):
    """Multi-state OSD over the layer-shell QML scene."""

    def __init__(self) -> None:
        super().__init__()
        self._state = _OSDState()
        self._engine = QQmlApplicationEngine()
        for p in ("/usr/lib/qt6/qml", "/usr/lib64/qt6/qml"):
            if os.path.isdir(p):
                self._engine.addImportPath(p)
        self._engine.rootContext().setContextProperty("osdState", self._state)
        self._engine.load(str(_QML_PATH))
        if not self._engine.rootObjects():
            raise RuntimeError(f"Failed to load OSD QML from {_QML_PATH}")

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)

    # ---- API ----------------------------------------------------------

    def map_offscreen(self) -> None:
        # Layer-shell handles non-focus-stealing semantics. No-op kept for
        # API parity with the previous QWidget OSD.
        pass

    def set_listening(self, write_mode: bool = False) -> None:
        """Mic just opened, no speech yet. Show localized placeholder
        with a blinking cursor."""
        self._state.prompt = t("listening_placeholder")
        self._state.flux = ""
        self._state.status = t("write_mode_on") if write_mode else ""
        self._state.show_cursor = True
        self._state.placeholder_mode = True
        self._state.visible = True
        self._hide_timer.stop()

    def set_partial(self, text: str, *, flux: str = "", write_mode: bool = False) -> None:
        """Streaming partial transcript.

        ``text`` is the locked-prefix region — words that have stayed
        identical across recent partials and are unlikely to be revised.
        ``flux`` is the still-changing tail of the same partial; the QML
        renders it at a faded color so the eye knows what's settled.

        Pass ``flux=""`` (the default) to render a single bright line —
        useful for the very first partial or when nothing has been
        locked yet.
        """
        self._state.prompt = text or ""
        self._state.flux = flux or ""
        self._state.status = t("write_mode_on") if write_mode else ""
        self._state.show_cursor = False
        self._state.placeholder_mode = False
        self._state.visible = True
        self._hide_timer.stop()

    def set_thinking(self, prompt: str, hint: str = "") -> None:
        """LLM is reasoning. Keep the user's prompt visible; show a faded
        thinking hint below."""
        self._state.prompt = prompt or ""
        self._state.flux = ""
        self._state.status = hint or t("thinking")
        self._state.show_cursor = False
        self._state.placeholder_mode = False
        self._state.visible = True
        self._hide_timer.stop()

    def set_executing(self, prompt: str, what: str = "") -> None:
        """Action is firing. Show a faded execution hint below the prompt."""
        self._state.prompt = prompt or ""
        self._state.flux = ""
        if what:
            self._state.status = f"{t('executing')}: {what}"
        else:
            self._state.status = t("executing")
        self._state.show_cursor = False
        self._state.placeholder_mode = False
        self._state.visible = True
        self._hide_timer.stop()

    def set_pending(self, prompt_so_far: str, hint: str = "") -> None:
        """Pending parameterized action — show what was said + a hint
        for the missing parameter, with a blinking cursor."""
        self._state.prompt = prompt_so_far or ""
        self._state.flux = ""
        self._state.status = hint or t("pending_param_hint")
        self._state.show_cursor = True
        self._state.placeholder_mode = False
        self._state.visible = True
        self._hide_timer.stop()

    def set_committed(self, text: str, *, transient_ms: int = 0) -> None:
        """Final committed text. Status line clears."""
        self._state.prompt = text or ""
        self._state.flux = ""
        self._state.status = ""
        self._state.show_cursor = False
        self._state.placeholder_mode = False
        self._state.visible = True
        if transient_ms > 0:
            self._hide_timer.start(transient_ms)
        else:
            self._hide_timer.stop()

    def hide_after(self, ms: int) -> None:
        self._hide_timer.start(ms)

    def hide_now(self) -> None:
        self._hide_timer.stop()
        self._fade_out()

    # ---- Backward compat for existing callers --------------------------

    def show_text(self, text: str, *, transient_ms: int = 0) -> None:
        """Legacy single-line API. Maps to set_committed."""
        self.set_committed(text, transient_ms=transient_ms)

    # ---- Internal ------------------------------------------------------

    def _fade_out(self) -> None:
        self._state.visible = False
