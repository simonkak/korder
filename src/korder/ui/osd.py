from __future__ import annotations
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPaintEvent
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class _Card(QWidget):
    """Rounded translucent card. Painted only when visible."""

    def __init__(self) -> None:
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(QColor(20, 22, 30, 230))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(self.rect(), 14, 14)


class OSDWindow(QWidget):
    """Frameless click-through OSD that stays mapped throughout app lifetime;
    visible content toggles by show/hide on the inner card, not the window."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._card = _Card()
        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(24, 14, 24, 14)

        self._label = QLabel("")
        self._label.setWordWrap(True)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet(
            "color: white; font-size: 16pt; font-weight: 500; background: transparent;"
        )
        card_layout.addWidget(self._label)
        outer.addWidget(self._card)

        self.setFixedWidth(720)
        self.setMinimumHeight(64)
        self._card.hide()

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._card.hide)

    def map_offscreen(self) -> None:
        """Map the Wayland surface once at app start. The card stays hidden until
        the first show_text(). Subsequent show/hide of the card don't re-map."""
        if not self.isVisible():
            self._reposition()
            self.show()

    def show_text(self, text: str, *, transient_ms: int = 0) -> None:
        self._label.setText(text or "")
        self._reposition()
        if not self._card.isVisible():
            self._card.show()
        if transient_ms > 0:
            self._hide_timer.start(transient_ms)
        else:
            self._hide_timer.stop()

    def hide_after(self, ms: int) -> None:
        self._hide_timer.start(ms)

    def hide_now(self) -> None:
        self._hide_timer.stop()
        self._card.hide()

    def _reposition(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.adjustSize()
        x = geo.x() + (geo.width() - self.width()) // 2
        y = geo.y() + (geo.height() * 2 // 3) - (self.height() // 2)
        self.move(x, y)
