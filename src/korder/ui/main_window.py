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

import time

from korder.actions.base import get_action
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


class _InjectWorker(QThread):
    """Parses + filters + executes ops off the UI thread so the LLM call
    (300-500ms with Gemma) doesn't freeze the OSD. Tracks write-mode state
    locally and emits mode_changed / pending_action back to main thread
    when toggle / parameterized-incomplete ops fire."""

    done = Signal(str)  # carries the original text for post-inject UI update
    failed = Signal(str)
    mode_changed = Signal(bool)  # True = write mode on, False = off
    pending_action = Signal(str)  # name of an action waiting for a param
    command_executed = Signal()  # at least one non-text, non-mode action ran

    def __init__(
        self,
        injector: YdotoolBackend,
        payload: str,
        original: str,
        initial_write_mode: bool,
        prebuilt_ops: list | None = None,
    ):
        super().__init__()
        self._injector = injector
        self._payload = payload
        self._original = original
        self._write_mode = initial_write_mode
        self._prebuilt_ops = prebuilt_ops

    def run(self) -> None:
        try:
            if self._prebuilt_ops is not None:
                ops = self._prebuilt_ops
            else:
                ops = self._injector.parse_ops(self._payload)
            filtered: list[tuple] = []
            had_command_action = False
            for op in ops:
                kind = op[0]
                if kind == "write_mode":
                    new_mode = bool(op[1])
                    if new_mode != self._write_mode:
                        self._write_mode = new_mode
                        self.mode_changed.emit(new_mode)
                elif kind == "pending_action":
                    # Don't execute; main thread will store and use the
                    # next text commit as the parameter.
                    self.pending_action.emit(op[1])
                elif kind in ("text", "char"):
                    if self._write_mode:
                        filtered.append(op)
                else:
                    # key, combo, subprocess, callable: always execute,
                    # and counts as a "command" for auto-stop purposes.
                    filtered.append(op)
                    had_command_action = True
            self._injector.execute_ops(filtered)
            if had_command_action:
                self.command_executed.emit()
            self.done.emit(self._original)
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
        auto_stop_after_action: bool = True,
    ):
        super().__init__()
        self.setWindowTitle("Korder — transcript history")
        self.resize(560, 360)

        self._engine = engine
        self._recorder = recorder
        self._injector = injector
        self._osd = osd
        self._trailing_space = trailing_space
        self._auto_stop_after_action = auto_stop_after_action
        self._auto_stop_pending = False
        self._detector = SpeechDetector(sample_rate=recorder.sample_rate, aggressiveness=3)
        self._workers: set[_TranscribeWorker] = set()
        self._inject_workers: set[_InjectWorker] = set()
        self._commit_queue: list[str] = []
        self._write_mode = False  # default: preview only, no typing
        # When the LLM detects an action with empty required params (e.g.,
        # spotify_search with no query), we store the action name + time
        # and treat the NEXT text-only commit as the parameter.
        self._pending_action: str | None = None
        self._pending_action_time: float = 0.0
        self.PENDING_ACTION_TIMEOUT_S = 8.0
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

    PAUSE_MS = 3500
    MIN_COMMIT_MS = 500
    MAX_SEGMENT_MS = 18000
    MIN_SPEECH_FOR_PARTIAL_MS = 90
    STABILITY_REPEATS = 8  # commit after this many identical-content partials in a row

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
        """Called from app shutdown to flush in-flight workers."""
        for w in list(self._workers):
            w.wait(2000)
        for w in list(self._inject_workers):
            w.wait(2000)

    def _on_commit_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            if not self._recorder.is_recording:
                self._status.setText("Idle.")
                self._osd.hide_after(300)
            return

        # Serialize commits while inject workers are in flight. Without
        # this, a follow-up commit can start its own LLM parse before the
        # previous worker emits pending_action — losing the "next text
        # commit is my parameter" wiring. _reap_inject drains the queue.
        if self._inject_workers:
            self._commit_queue.append(text)
            return

        self._process_commit(text)

    def _process_commit(self, text: str) -> None:
        # If a parameterized action is waiting for its input and we're
        # within the timeout, take this commit's text as the parameter.
        if self._pending_action is not None:
            elapsed = time.time() - self._pending_action_time
            if elapsed <= self.PENDING_ACTION_TIMEOUT_S:
                pending = self._pending_action
                self._pending_action = None
                if self._resolve_pending_action(pending, text):
                    self._transcript.appendPlainText(text)
                    return
            else:
                # Timeout — drop the pending state and fall through.
                self._pending_action = None

        self._transcript.appendPlainText(text)
        injecting = self._inject_chk.isChecked() and self._injector is not None

        if injecting:
            # Show thinking + mode marker while inject (and possible LLM
            # parse) runs in a worker. ✏️ = write mode on, 👁 = preview only.
            mode_glyph = "✏️ " if self._write_mode else "👁 "
            slow = "💭 " if self._injector.is_slow_parser else ""
            self._osd.show_text(f"{slow}{mode_glyph}{text}")
            payload = text + (" " if self._trailing_space else "")
            worker = _InjectWorker(self._injector, payload, text, self._write_mode)
            worker.done.connect(self._on_inject_done)
            worker.failed.connect(self._on_inject_failed)
            worker.mode_changed.connect(self._on_mode_changed)
            worker.pending_action.connect(self._on_pending_action)
            worker.command_executed.connect(self._on_command_executed)
            worker.finished.connect(lambda w=worker: self._reap_inject(w))
            self._inject_workers.add(worker)
            worker.start()
        else:
            if self._recorder.is_recording:
                self._status.setText("Listening...")
                self._osd.show_text(text)
            else:
                self._status.setText("Idle.")
                self._osd.show_text(text, transient_ms=1500)

        if self._recorder.is_recording:
            self._status.setText("Listening...")
        else:
            self._status.setText("Idle.")

    def _on_inject_done(self, original_text: str) -> None:
        # Post-inject: replace the marker'd text with mode-prefixed text.
        mode_glyph = "✏️ " if self._write_mode else "👁 "
        decorated = f"{mode_glyph}{original_text}"
        if self._recorder.is_recording:
            self._osd.show_text(decorated)
        else:
            self._osd.show_text(decorated, transient_ms=1500)

        # Auto-stop if a command-style action just ran. Deferred so the
        # OSD's post-execution frame renders before recording stops.
        if self._auto_stop_pending and self._auto_stop_after_action:
            self._auto_stop_pending = False
            if self._recorder.is_recording:
                QTimer.singleShot(150, self._auto_stop)

    def _on_inject_failed(self, msg: str) -> None:
        self.statusBar().showMessage(f"Inject failed: {msg}", 8000)
        self._auto_stop_pending = False

    def _on_command_executed(self) -> None:
        """Worker reports a non-text, non-mode-toggle action ran. Flag set
        so _on_inject_done can stop recording after the UI update lands."""
        self._auto_stop_pending = True

    def _auto_stop(self) -> None:
        """Quietly stop recording — used after a command finishes."""
        if self._recorder.is_recording:
            print("[korder] auto-stop after command", flush=True)
            self._stop_recording()
            self._sync_button()

    def _on_mode_changed(self, write_mode: bool) -> None:
        """Worker reports the user toggled write mode mid-commit."""
        self._write_mode = write_mode
        self.statusBar().showMessage(
            "Write mode ON" if write_mode else "Write mode OFF (preview only)",
            3000,
        )

    def _on_pending_action(self, action_name: str) -> None:
        """Worker reports a parameterized action with empty params —
        wait for the next commit and treat it as the parameter."""
        self._pending_action = action_name
        self._pending_action_time = time.time()
        action = get_action(action_name)
        param_label = ""
        if action and action.parameters:
            param_label = next(iter(action.parameters.keys()))
        self._osd.show_text(f"⏳ {action_name} → say {param_label}…")
        self.statusBar().showMessage(
            f"Pending: {action_name} waiting for parameter",
            int(self.PENDING_ACTION_TIMEOUT_S * 1000),
        )

    def _resolve_pending_action(self, action_name: str, param_text: str) -> bool:
        """Build params from the follow-up text and dispatch the action.
        Returns True on successful dispatch, False if the action couldn't
        be resolved (caller should fall through to normal flow)."""
        action = get_action(action_name)
        if action is None or not action.parameters:
            return False
        first_param = next(iter(action.parameters.keys()))
        op = action.op_factory({first_param: param_text})
        if op is None or self._injector is None:
            return False
        # Run on a worker thread so the LLM-free path here doesn't block UI.
        self._osd.show_text(f"💭 {action_name}: {param_text}")
        worker = _InjectWorker(
            self._injector, "", param_text, self._write_mode,
            prebuilt_ops=[op],
        )
        worker.done.connect(self._on_inject_done)
        worker.failed.connect(self._on_inject_failed)
        worker.mode_changed.connect(self._on_mode_changed)
        worker.pending_action.connect(self._on_pending_action)
        worker.command_executed.connect(self._on_command_executed)
        worker.finished.connect(lambda w=worker: self._reap_inject(w))
        self._inject_workers.add(worker)
        worker.start()
        return True

    def _reap_inject(self, worker: _InjectWorker) -> None:
        self._inject_workers.discard(worker)
        worker.deleteLater()
        # Drain queued commits one at a time, waiting for the previous
        # worker to fully finish before starting the next.
        if not self._inject_workers and self._commit_queue:
            next_text = self._commit_queue.pop(0)
            self._process_commit(next_text)

    def _on_fail(self, msg: str) -> None:
        self._status.setText(f"Transcription failed: {msg}")


def _normalize_for_compare(text: str) -> str:
    """Lowercase + strip non-word chars so 'Hello, world' == 'hello world.'"""
    return re.sub(r"[^\w\s]", "", text.lower()).strip()
