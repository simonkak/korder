from __future__ import annotations
import ctypes
import os
from pathlib import Path
from PySide6.QtCore import QObject, Property, QTimer, Signal
from PySide6.QtGui import QRegion
from PySide6.QtQml import QQmlApplicationEngine

_QML_PATH = Path(__file__).parent / "qml" / "osd.qml"


# Wrap KWindowEffects::enableBlurBehind from libKF6WindowSystem so KWin
# applies a gaussian blur behind the OSD's translucent regions. This is a
# C++ function — there's no QML or Python binding for it, so we call the
# mangled symbol via ctypes and pass shiboken-extracted pointers.
def _load_blur_fn():
    try:
        lib = ctypes.CDLL("libKF6WindowSystem.so.6")
        fn = getattr(lib, "_ZN14KWindowEffects16enableBlurBehindEP7QWindowbRK7QRegion")
        fn.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_void_p]
        fn.restype = None
        return fn
    except (OSError, AttributeError):
        return None


_ENABLE_BLUR = _load_blur_fn()


def _request_blur(window) -> bool:
    if _ENABLE_BLUR is None:
        return False
    try:
        import shiboken6
    except ImportError:
        return False
    region = QRegion()  # empty region = blur the entire window
    win_ptr = shiboken6.getCppPointer(window)[0]
    region_ptr = shiboken6.getCppPointer(region)[0]
    _ENABLE_BLUR(win_ptr, True, region_ptr)
    return True


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
    visibility toggles via inner-rectangle binding. Same API as the old
    QWidget version so MainWindow doesn't need to change."""

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

        # Ask KWin to blur the screen content behind our window.
        # Defer to next event-loop tick so the Wayland surface is created.
        QTimer.singleShot(0, self._apply_blur)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)

    def map_offscreen(self) -> None:
        # Layer-shell handles non-focus-stealing semantics at the protocol
        # level, so no startup mapping trick is needed. Kept for API parity
        # with the previous QWidget OSD.
        pass

    def _apply_blur(self) -> None:
        roots = self._engine.rootObjects()
        if not roots:
            return
        ok = _request_blur(roots[0])
        if not ok:
            print("[korder] KWin blur unavailable (libKF6WindowSystem missing or shiboken6 absent)", flush=True)

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
