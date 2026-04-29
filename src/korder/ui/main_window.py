from __future__ import annotations
import numpy as np
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QPlainTextEdit,
    QLabel,
    QStatusBar,
    QCheckBox,
)

from korder.audio.capture import MicRecorder
from korder.transcribe.whisper_engine import WhisperEngine
from korder.inject import YdotoolBackend, InjectError


class _TranscribeWorker(QThread):
    finished_text = Signal(str)
    failed = Signal(str)

    def __init__(self, engine: WhisperEngine, audio: np.ndarray):
        super().__init__()
        self._engine = engine
        self._audio = audio

    def run(self) -> None:
        try:
            text = self._engine.transcribe(self._audio)
            self.finished_text.emit(text)
        except Exception as e:
            self.failed.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        engine: WhisperEngine,
        recorder: MicRecorder,
        injector: YdotoolBackend | None,
        stay_on_top: bool = True,
        non_focusable: bool = True,
        trailing_space: bool = True,
    ):
        super().__init__()
        self.setWindowTitle("Korder")
        self.resize(560, 260)

        flags = Qt.WindowType.Tool
        if stay_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        if non_focusable:
            flags |= Qt.WindowType.WindowDoesNotAcceptFocus
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, True)

        self._engine = engine
        self._recorder = recorder
        self._injector = injector
        self._trailing_space = trailing_space
        self._workers: set[_TranscribeWorker] = set()

        central = QWidget()
        layout = QVBoxLayout(central)

        self._status = QLabel("Idle. Hold the button to dictate.")
        layout.addWidget(self._status)

        self._transcript = QPlainTextEdit()
        self._transcript.setPlaceholderText("Transcripts will accumulate here...")
        layout.addWidget(self._transcript)

        bar = QHBoxLayout()
        self._ptt = QPushButton("Hold to talk")
        self._ptt.pressed.connect(self._on_press)
        self._ptt.released.connect(self._on_release)
        bar.addWidget(self._ptt)

        self._inject_chk = QCheckBox("Inject into focused app")
        self._inject_chk.setChecked(injector is not None)
        if injector is None:
            self._inject_chk.setEnabled(False)
            self._inject_chk.setToolTip(
                "ydotool unavailable. Install it and run scripts/setup-uinput.sh."
            )
        bar.addWidget(self._inject_chk)
        layout.addLayout(bar)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

    def _on_press(self) -> None:
        if self._recorder.is_recording:
            return
        try:
            self._recorder.start()
        except Exception as e:
            self.statusBar().showMessage(f"Mic error: {e}", 6000)
            return
        self._status.setText("Recording... release to transcribe.")

    def _on_release(self) -> None:
        if not self._recorder.is_recording:
            return
        audio = self._recorder.stop()
        if audio.size < int(0.2 * self._recorder.sample_rate):
            self._status.setText("Too short, try again.")
            return
        self._status.setText("Transcribing...")
        worker = _TranscribeWorker(self._engine, audio)
        worker.finished_text.connect(self._on_text)
        worker.failed.connect(self._on_fail)
        worker.finished.connect(lambda w=worker: self._reap(w))
        self._workers.add(worker)
        worker.start()

    def _reap(self, worker: _TranscribeWorker) -> None:
        self._workers.discard(worker)
        worker.deleteLater()

    def closeEvent(self, event: QCloseEvent) -> None:
        for w in list(self._workers):
            w.wait(2000)
        super().closeEvent(event)

    def _on_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            self._status.setText("(no speech detected)")
            return
        self._transcript.appendPlainText(text)
        self._status.setText("Idle.")
        if self._inject_chk.isChecked() and self._injector is not None:
            payload = text + (" " if self._trailing_space else "")
            try:
                self._injector.type(payload)
            except InjectError as e:
                self.statusBar().showMessage(f"Inject failed: {e}", 8000)

    def _on_fail(self, msg: str) -> None:
        self._status.setText(f"Transcription failed: {msg}")
