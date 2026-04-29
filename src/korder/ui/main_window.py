from __future__ import annotations
import re
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
from korder.audio.vad import SpeechDetector
from korder.transcribe.whisper_engine import WhisperEngine
from korder.inject import YdotoolBackend, InjectError
from korder.ui.osd import OSDWindow


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
        osd: OSDWindow,
        trailing_space: bool = True,
    ):
        super().__init__()
        self.setWindowTitle("Korder — transcript history")
        self.resize(560, 360)

        self._engine = engine
        self._recorder = recorder
        self._injector = injector
        self._osd = osd
        self._trailing_space = trailing_space
        self._detector = SpeechDetector(sample_rate=recorder.sample_rate, aggressiveness=3)
        self._workers: set[_TranscribeWorker] = set()
        self._partial_in_flight = False
        self._committed_samples = 0
        self._last_partial_norm = ""
        self._stability_count = 0
        self._partial_timer = QTimer(self)
        self._partial_timer.setInterval(100)
        self._partial_timer.timeout.connect(self._on_partial_tick)

        central = QWidget()
        layout = QVBoxLayout(central)

        self._status = QLabel("Idle. Use the tray icon or hotkey to dictate.")
        layout.addWidget(self._status)

        self._transcript = QPlainTextEdit()
        self._transcript.setPlaceholderText("Transcripts will accumulate here...")
        self._transcript.setReadOnly(True)
        layout.addWidget(self._transcript)

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

    PAUSE_MS = 1000
    MIN_COMMIT_MS = 500
    MAX_SEGMENT_MS = 12000
    MIN_SPEECH_FOR_PARTIAL_MS = 90
    STABILITY_REPEATS = 2  # commit after this many identical-content partials in a row

    def _start_recording(self) -> None:
        if self._recorder.is_recording:
            return
        try:
            self._recorder.start()
        except Exception as e:
            self.statusBar().showMessage(f"Mic error: {e}", 6000)
            return
        self._status.setText("Listening...")
        self._osd.show_text("Listening…")
        self._partial_in_flight = False
        self._committed_samples = 0
        self._last_partial_norm = ""
        self._stability_count = 0
        self._partial_timer.start()

    def _stop_recording(self) -> None:
        if not self._recorder.is_recording:
            return
        self._partial_timer.stop()
        full = self._recorder.stop()
        sr = self._recorder.sample_rate
        remaining = full[self._committed_samples:]
        if remaining.size < int(0.2 * sr) or not self._detector.has_speech(remaining):
            self._status.setText("Idle.")
            self._osd.hide_after(300)
            return
        self._status.setText("Transcribing...")
        self._osd.show_text("Transcribing…")
        self._submit_transcribe(remaining, kind="commit")

    def _on_partial_tick(self) -> None:
        if not self._recorder.is_recording:
            return
        sr = self._recorder.sample_rate
        full = self._recorder.snapshot()
        new = full[self._committed_samples:]
        if new.size < int(0.3 * sr):
            return

        speech_end, silence_ms = self._detector.find_trailing_silence(new)
        speech_min = int((self.MIN_COMMIT_MS / 1000) * sr)

        commit_on_pause = silence_ms >= self.PAUSE_MS and speech_end >= speech_min
        commit_on_max = new.size >= int((self.MAX_SEGMENT_MS / 1000) * sr) and speech_end >= speech_min

        if commit_on_pause or commit_on_max:
            segment = np.ascontiguousarray(new[:speech_end])
            self._committed_samples += speech_end
            self._last_partial_norm = ""
            self._stability_count = 0
            self._submit_transcribe(segment, kind="commit")
            return

        if not self._partial_in_flight and self._detector.has_speech(
            new, min_speech_ms=self.MIN_SPEECH_FOR_PARTIAL_MS
        ):
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
        if not text:
            return
        self._osd.show_text(text)

        # Stability-based commit: if the model returns the same content twice
        # in a row, the audio buffer's content has converged — user paused.
        norm = _normalize_for_compare(text)
        if norm and norm == self._last_partial_norm:
            self._stability_count += 1
            if self._stability_count >= self.STABILITY_REPEATS:
                self._commit_via_stability()
        else:
            self._stability_count = 1
            self._last_partial_norm = norm

    def _commit_via_stability(self) -> None:
        if not self._recorder.is_recording:
            return
        sr = self._recorder.sample_rate
        full = self._recorder.snapshot()
        new = full[self._committed_samples:]
        speech_min = int((self.MIN_COMMIT_MS / 1000) * sr)
        if new.size < speech_min:
            return
        speech_end, _ = self._detector.find_trailing_silence(new)
        if speech_end < speech_min:
            speech_end = new.size
        segment = np.ascontiguousarray(new[:speech_end])
        self._committed_samples += speech_end
        self._last_partial_norm = ""
        self._stability_count = 0
        self._submit_transcribe(segment, kind="commit")

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
        # Tray-first design: closing the history window just hides it.
        # Quit happens via the tray menu, which fires QApplication.quit().
        event.ignore()
        self.hide()

    def shutdown(self) -> None:
        """Called from app shutdown to flush in-flight transcribe workers."""
        for w in list(self._workers):
            w.wait(2000)

    def _on_commit_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            if not self._recorder.is_recording:
                self._status.setText("Idle.")
                self._osd.hide_after(300)
            return
        self._transcript.appendPlainText(text)
        if self._recorder.is_recording:
            self._status.setText("Listening...")
            self._osd.show_text(text)
        else:
            self._status.setText("Idle.")
            self._osd.show_text(text, transient_ms=1500)
        if self._inject_chk.isChecked() and self._injector is not None:
            payload = text + (" " if self._trailing_space else "")
            try:
                self._injector.type(payload)
            except InjectError as e:
                self.statusBar().showMessage(f"Inject failed: {e}", 8000)

    def _on_fail(self, msg: str) -> None:
        self._status.setText(f"Transcription failed: {msg}")


def _normalize_for_compare(text: str) -> str:
    """Lowercase + strip non-word chars so 'Hello, world' == 'hello world.'"""
    return re.sub(r"[^\w\s]", "", text.lower()).strip()
