from __future__ import annotations
import numpy as np
from PySide6.QtCore import Qt, QThread, QTimer, Signal
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
        self._partial_in_flight = False
        self._committed_samples = 0
        self._partial_timer = QTimer(self)
        self._partial_timer.setInterval(300)
        self._partial_timer.timeout.connect(self._on_partial_tick)

        central = QWidget()
        layout = QVBoxLayout(central)

        self._status = QLabel("Idle. Click the button (or press your hotkey) to dictate.")
        layout.addWidget(self._status)

        self._transcript = QPlainTextEdit()
        self._transcript.setPlaceholderText("Transcripts will accumulate here...")
        layout.addWidget(self._transcript)

        self._live = QLabel("")
        self._live.setWordWrap(True)
        self._live.setStyleSheet(
            "color: palette(mid); font-style: italic; padding: 4px;"
        )
        self._live.setMinimumHeight(28)
        layout.addWidget(self._live)

        bar = QHBoxLayout()
        self._ptt = QPushButton("Start dictating")
        self._ptt.setCheckable(True)
        self._ptt.clicked.connect(lambda _checked: self.toggle_recording())
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

    def toggle_recording(self) -> None:
        if self._recorder.is_recording:
            self._stop_recording()
        else:
            self._start_recording()
        self._sync_button()

    PAUSE_MS = 500
    MIN_COMMIT_MS = 600
    SILENCE_THRESHOLD = 0.005

    def _start_recording(self) -> None:
        if self._recorder.is_recording:
            return
        try:
            self._recorder.start()
        except Exception as e:
            self.statusBar().showMessage(f"Mic error: {e}", 6000)
            return
        self._status.setText("Listening...")
        self._live.setText("")
        self._partial_in_flight = False
        self._committed_samples = 0
        self._partial_timer.start()

    def _stop_recording(self) -> None:
        if not self._recorder.is_recording:
            return
        self._partial_timer.stop()
        full = self._recorder.stop()
        self._live.setText("")
        sr = self._recorder.sample_rate
        remaining = full[self._committed_samples:]
        if remaining.size < int(0.2 * sr):
            self._status.setText("Idle.")
            return
        self._status.setText("Transcribing...")
        self._submit_transcribe(remaining, kind="commit")

    def _on_partial_tick(self) -> None:
        if not self._recorder.is_recording:
            return
        sr = self._recorder.sample_rate
        full = self._recorder.snapshot()
        new = full[self._committed_samples:]
        if new.size < int(0.5 * sr):
            return

        tail_window = int((self.PAUSE_MS / 1000) * sr)
        speech_min = int((self.MIN_COMMIT_MS / 1000) * sr)
        if new.size > tail_window:
            tail = new[-tail_window:]
            tail_rms = float(np.sqrt(np.mean(tail.astype(np.float32) ** 2)))
            has_pause = tail_rms < self.SILENCE_THRESHOLD and (new.size - tail_window) >= speech_min
        else:
            has_pause = False

        if has_pause:
            speech_end = new.size - tail_window
            segment = np.ascontiguousarray(new[:speech_end])
            self._committed_samples += speech_end
            self._live.setText("")
            self._submit_transcribe(segment, kind="commit")
            return

        if not self._partial_in_flight:
            self._submit_transcribe(np.ascontiguousarray(new), kind="partial")

    def _submit_transcribe(self, audio: np.ndarray, kind: str) -> None:
        worker = _TranscribeWorker(self._engine, audio)
        if kind == "partial":
            self._partial_in_flight = True
            worker.finished_text.connect(self._on_partial_text)
            worker.failed.connect(self._on_partial_fail)
        else:
            worker.finished_text.connect(self._on_commit_text)
            worker.failed.connect(self._on_fail)
        worker.finished.connect(lambda w=worker: self._reap(w))
        self._workers.add(worker)
        worker.start()

    def _on_partial_text(self, text: str) -> None:
        self._partial_in_flight = False
        if not self._recorder.is_recording:
            return
        text = text.strip()
        if text:
            self._live.setText(text)

    def _on_partial_fail(self, _msg: str) -> None:
        self._partial_in_flight = False

    def _sync_button(self) -> None:
        rec = self._recorder.is_recording
        self._ptt.blockSignals(True)
        self._ptt.setChecked(rec)
        self._ptt.blockSignals(False)
        self._ptt.setText("Stop" if rec else "Start dictating")

    def _reap(self, worker: _TranscribeWorker) -> None:
        self._workers.discard(worker)
        worker.deleteLater()

    def closeEvent(self, event: QCloseEvent) -> None:
        for w in list(self._workers):
            w.wait(2000)
        super().closeEvent(event)

    def _on_commit_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            if not self._recorder.is_recording:
                self._status.setText("Idle.")
            return
        self._transcript.appendPlainText(text)
        if self._recorder.is_recording:
            self._status.setText("Listening...")
        else:
            self._status.setText("Idle.")
        if self._inject_chk.isChecked() and self._injector is not None:
            payload = text + (" " if self._trailing_space else "")
            try:
                self._injector.type(payload)
            except InjectError as e:
                self.statusBar().showMessage(f"Inject failed: {e}", 8000)

    def _on_fail(self, msg: str) -> None:
        self._status.setText(f"Transcription failed: {msg}")
