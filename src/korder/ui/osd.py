from __future__ import annotations
import os
from pathlib import Path
from PySide6.QtCore import QObject, Property, QTimer, Signal
from PySide6.QtQml import QQmlApplicationEngine

_QML_PATH = Path(__file__).parent / "qml" / "osd.qml"


class _OSDState(QObject):
    """Plain-data QObject exposed to the QML scene as `osdState`."""

    textChanged = Signal()
    visibleChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._text = ""
        self._visible = False

    def _get_text(self) -> str:
        return self._text

    def _set_text(self, value: str) -> None:
        if self._text != value:
            self._text = value
            self.textChanged.emit()

    text = Property(str, _get_text, _set_text, notify=textChanged)

    def _get_visible(self) -> bool:
        return self._visible

    def _set_visible(self, value: bool) -> None:
        if self._visible != value:
            self._visible = value
            self.visibleChanged.emit()

    visible = Property(bool, _get_visible, _set_visible, notify=visibleChanged)


class OSDWindow(QObject):
    """Layer-shell OSD via QML. Surface stays mapped throughout app lifetime;
    visibility toggles via inner-rectangle binding. Same API as the original
    QWidget OSD so MainWindow doesn't need to change."""

    def __init__(self) -> None:
        super().__init__()
        self._state = _OSDState()
        self._engine = QQmlApplicationEngine()
        # PySide6 bundles its own Qt6 with a separate QML import path; the
        # system's KDE QML plugins (including org.kde.layershell) live under
        # /usr/lib/qt6/qml. Adding that path lets us load them from PySide6.
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

    def map_offscreen(self) -> None:
        # Layer-shell handles non-focus-stealing semantics at the protocol
        # level. Kept for API parity with the previous QWidget OSD.
        pass

    def show_text(self, text: str, *, transient_ms: int = 0) -> None:
        self._state.text = text or ""
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

    def _fade_out(self) -> None:
        self._state.visible = False
