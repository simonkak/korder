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
    stateLabelChanged = Signal()
    stateKindChanged = Signal()
    feedbackModeChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._prompt = ""
        self._flux = ""
        self._status = ""
        self._visible = False
        self._show_cursor = False
        self._placeholder_mode = False
        # Leading-section state. `state_label` is the user-visible word
        # ("Listening", "Thinking", …); `state_kind` is the compact tag the
        # QML uses to pick icon + accent (one of: idle, listening, thinking,
        # executing, pending, committed).
        self._state_label = ""
        self._state_kind = "idle"
        # When True, the center prompt is system feedback (action progress
        # narration) — render in the state's accent color + italic so it
        # reads visually distinct from the user's own dictated words.
        self._feedback_mode = False
        # Localized "Press ESC to cancel" hint, set once at startup.
        self._esc_hint = t("press_to_cancel")

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

    def _get_state_label(self) -> str:
        return self._state_label

    def _set_state_label(self, value: str) -> None:
        if self._state_label != value:
            self._state_label = value
            self.stateLabelChanged.emit()

    stateLabel = Property(str, _get_state_label, _set_state_label, notify=stateLabelChanged)

    def _get_state_kind(self) -> str:
        return self._state_kind

    def _set_state_kind(self, value: str) -> None:
        if self._state_kind != value:
            self._state_kind = value
            self.stateKindChanged.emit()

    stateKind = Property(str, _get_state_kind, _set_state_kind, notify=stateKindChanged)

    def _get_feedback_mode(self) -> bool:
        return self._feedback_mode

    def _set_feedback_mode(self, value: bool) -> None:
        if self._feedback_mode != value:
            self._feedback_mode = value
            self.feedbackModeChanged.emit()

    feedbackMode = Property(bool, _get_feedback_mode, _set_feedback_mode, notify=feedbackModeChanged)

    def _get_esc_hint(self) -> str:
        return self._esc_hint

    # Constant after startup — no notify signal needed; QML reads it once.
    escHint = Property(str, _get_esc_hint, constant=True)


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
        self._state.stateLabel = t("state_listening")
        self._state.stateKind = "listening"
        self._state.showCursor = True
        self._state.placeholderMode = True
        self._state.feedbackMode = False
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
        self._state.stateLabel = t("state_listening")
        self._state.stateKind = "listening"
        self._state.showCursor = False
        self._state.placeholderMode = False
        self._state.feedbackMode = False
        self._state.visible = True
        self._hide_timer.stop()

    def set_thinking(self, prompt: str, hint: str = "") -> None:
        """LLM is reasoning. Keep the user's prompt visible; show the
        explicit hint (if any) in the trailing chip — the leading state
        label already says "Thinking" so the default would be redundant."""
        self._state.prompt = prompt or ""
        self._state.flux = ""
        self._state.status = hint  # only render trailing if caller passed something concrete
        self._state.stateLabel = t("state_thinking")
        self._state.stateKind = "thinking"
        self._state.showCursor = False
        self._state.placeholderMode = False
        self._state.feedbackMode = False
        self._state.visible = True
        self._hide_timer.stop()

    def set_executing(self, prompt: str, what: str = "") -> None:
        """Action is firing. The trailing chip shows the action name when
        passed (e.g. 'spotify_play'); empty otherwise — the leading state
        label already says "Executing" so the bare default is redundant."""
        self._state.prompt = prompt or ""
        self._state.flux = ""
        self._state.status = what  # action name or nothing — never the bare verb
        self._state.stateLabel = t("state_executing")
        self._state.stateKind = "executing"
        self._state.showCursor = False
        self._state.placeholderMode = False
        self._state.feedbackMode = False
        self._state.visible = True
        self._hide_timer.stop()

    def set_executing_progress(self, text: str) -> None:
        """Update the center text mid-execution — narration like
        "Searching for X" / "Found album Y" / "Playing Y". State stays
        Executing; the leading label and pulse don't change. Caller is
        responsible for the final transition (typically set_committed)
        once the action completes.

        Sets feedbackMode=True so the QML renders this text in the
        state's accent color + italic, distinct from the user's own
        spoken command."""
        self._state.prompt = text or ""
        self._state.flux = ""
        self._state.status = ""
        self._state.stateLabel = t("state_executing")
        self._state.stateKind = "executing"
        self._state.showCursor = False
        self._state.placeholderMode = False
        self._state.feedbackMode = True
        self._state.visible = True
        self._hide_timer.stop()

    def set_pending(self, prompt_so_far: str, hint: str = "") -> None:
        """Pending parameterized action — show what was said + a hint
        for the missing parameter, with a blinking cursor."""
        self._state.prompt = prompt_so_far or ""
        self._state.flux = ""
        self._state.status = hint or t("pending_param_hint")
        self._state.stateLabel = t("state_pending")
        self._state.stateKind = "pending"
        self._state.showCursor = True
        self._state.placeholderMode = False
        self._state.feedbackMode = False
        self._state.visible = True
        self._hide_timer.stop()

    def set_committed(self, text: str, *, transient_ms: int = 0) -> None:
        """Final committed text. Status line clears."""
        self._state.prompt = text or ""
        self._state.flux = ""
        self._state.status = ""
        self._state.stateLabel = t("state_committed")
        self._state.stateKind = "committed"
        self._state.showCursor = False
        self._state.placeholderMode = False
        self._state.feedbackMode = False
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
