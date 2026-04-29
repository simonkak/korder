from __future__ import annotations
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPaintEvent
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class OSDWindow(QWidget):
    """A frameless, click-through, bottom-center toast for live transcription."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.SplashScreen
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 14, 24, 14)

        self._label = QLabel("")
        self._label.setWordWrap(True)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet(
            "color: white; font-size: 16pt; font-weight: 500;"
        )
        layout.addWidget(self._label)

        self.setFixedWidth(720)
        self.setMinimumHeight(64)

        self.setWindowOpacity(0.0)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)

    def map_offscreen(self) -> None:
        """Map the Wayland surface once at app start, kept invisible via opacity.
        Subsequent show_text calls just flip opacity, avoiding the focus-stealing
        re-map that would happen if we called show() per recording session."""
        if not self.isVisible():
            self._reposition()
            self.show()
            self.setWindowOpacity(0.0)

    def show_text(self, text: str, *, transient_ms: int = 0) -> None:
        self._label.setText(text or "")
        self._reposition()
        if not self.isVisible():
            self.show()
        if self.windowOpacity() < 1.0:
            self.setWindowOpacity(1.0)
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
        self.setWindowOpacity(0.0)

    def _reposition(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.adjustSize()
        x = geo.x() + (geo.width() - self.width()) // 2
        y = geo.y() + (geo.height() * 2 // 3) - (self.height() // 2)
        self.move(x, y)

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(QColor(20, 22, 30, 230))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(self.rect(), 14, 14)
