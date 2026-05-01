from __future__ import annotations
import os
import re
import sys
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
from korder.audio.ducker import VolumeDucker
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
    parse_started = Signal()  # LLM parse is about to start (slow path)
    parse_done = Signal()  # parse finished, executing now

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
                self.parse_started.emit()
                ops = self._injector.parse_ops(self._payload)
                self.parse_done.emit()
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
        ducker: VolumeDucker | None = None,
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
        # Always-present so call sites stay clean; defaults to disabled.
        self._ducker = ducker if ducker is not None else VolumeDucker(False, 30)
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
        self.PENDING_ACTION_TIMEOUT_S = 20.0
        self._partial_in_flight = False
        self._committed_samples = 0
        self._last_partial_norm = ""
        self._stability_count = 0
        self._partial_timer = QTimer(self)
        self._partial_timer.setInterval(100)
        self._partial_timer.timeout.connect(self._on_partial_tick)

        # OSD update throttle for streaming partials. Whisper sometimes
        # produces several partials per second; rendering each one makes
        # the prompt text dance. We rate-limit OSD updates to one per
        # OSD_PARTIAL_THROTTLE_MS, deferring the latest text until the
        # window opens. Stability detection still runs every partial —
        # this only smooths the visual.
        self._last_displayed_partial = ""        # for locked-prefix diff
        self._pending_partial_text: str | None = None
        self._last_osd_partial_t = 0.0           # monotonic seconds
        self._osd_throttle_timer = QTimer(self)
        self._osd_throttle_timer.setSingleShot(True)
        self._osd_throttle_timer.timeout.connect(self._flush_pending_partial)

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
    OSD_PARTIAL_THROTTLE_MS = 250  # max OSD update rate for streaming partials

    def _start_recording(self) -> None:
        if self._recorder.is_recording:
            return
        try:
            self._recorder.start()
        except Exception as e:
            self.statusBar().showMessage(f"Mic error: {e}", 6000)
            return
        self._ducker.duck()
        self._status.setText("Listening...")
        self._osd.set_listening(write_mode=self._write_mode)
        self._partial_in_flight = False
        self._committed_samples = 0
        self._last_partial_norm = ""
        self._stability_count = 0
        self._reset_partial_render_state()
        self._partial_timer.start()

    def _stop_recording(self) -> None:
        if not self._recorder.is_recording:
            return
        self._partial_timer.stop()
        self._osd_throttle_timer.stop()
        self._pending_partial_text = None
        full = self._recorder.stop()
        # Restore volume the moment the mic closes, regardless of whether
        # transcription proceeds or short-circuits below. Whisper runs
        # off-thread; we don't want playback held down for the duration.
        self._ducker.restore()
        sr = self._recorder.sample_rate
        remaining = full[self._committed_samples:]
        if remaining.size < int(0.2 * sr) or not self._detector.has_speech(remaining):
            self._status.setText("Idle.")
            self._osd.hide_after(300)
            return
        self._status.setText("Transcribing...")
        self._osd.set_thinking(self._osd._state.prompt or "", "transcribing…")
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
            self._reset_partial_render_state()
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

        # OSD update — rate-limited so the prompt doesn't dance every
        # 100 ms. Stability detection below runs unthrottled.
        self._maybe_render_partial(text)

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

    def _maybe_render_partial(self, text: str) -> None:
        """Throttled OSD update for streaming partials. If the throttle
        window is open we render now; otherwise we stash the text and arm
        a one-shot timer to flush when the window opens."""
        self._pending_partial_text = text
        elapsed_ms = (time.monotonic() - self._last_osd_partial_t) * 1000
        if elapsed_ms >= self.OSD_PARTIAL_THROTTLE_MS:
            self._flush_pending_partial()
        elif not self._osd_throttle_timer.isActive():
            delay_ms = max(0, int(self.OSD_PARTIAL_THROTTLE_MS - elapsed_ms))
            self._osd_throttle_timer.start(delay_ms)

    def _flush_pending_partial(self) -> None:
        text = self._pending_partial_text
        self._pending_partial_text = None
        if text is None or not self._recorder.is_recording:
            return
        locked, flux = _split_at_locked_prefix(self._last_displayed_partial, text)
        if os.environ.get("KORDER_DEBUG_OSD") == "1":
            print(
                f"[korder.osd] locked={locked!r} flux={flux!r} "
                f"(prev={self._last_displayed_partial!r}, curr={text!r})",
                file=sys.stderr,
                flush=True,
            )
        # No usable lock yet (first partial of the segment, or revision):
        # fall back to single-color render.
        if locked:
            self._osd.set_partial(locked, flux=flux, write_mode=self._write_mode)
        else:
            self._osd.set_partial(text, write_mode=self._write_mode)
        self._last_displayed_partial = text
        self._last_osd_partial_t = time.monotonic()

    def _reset_partial_render_state(self) -> None:
        """Clear the locked-prefix tracker. Called when starting a new
        recording or after every commit so the next partial isn't compared
        against text from a now-flushed segment."""
        self._last_displayed_partial = ""
        self._pending_partial_text = None
        self._last_osd_partial_t = 0.0
        self._osd_throttle_timer.stop()

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
        self._reset_partial_render_state()
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
        # Belt-and-braces: if quit fires while still recording, restore
        # the volume before workers wind down. atexit catches the rest.
        self._ducker.restore()
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
            print(
                f"[korder] commit {text!r} queued (workers in flight={len(self._inject_workers)}, pending={self._pending_action!r})",
                flush=True,
            )
            self._commit_queue.append(text)
            return

        print(
            f"[korder] commit {text!r} immediate (pending={self._pending_action!r})",
            flush=True,
        )
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
            # Keep the user's text bright at the top; worker signals
            # (parse_started / parse_done / command_executed) will set the
            # status hint underneath ("thinking", "executing", etc.).
            self._osd.set_committed(text)
            payload = text + (" " if self._trailing_space else "")
            worker = _InjectWorker(self._injector, payload, text, self._write_mode)
            worker.done.connect(self._on_inject_done)
            worker.failed.connect(self._on_inject_failed)
            worker.mode_changed.connect(self._on_mode_changed)
            worker.pending_action.connect(self._on_pending_action)
            worker.command_executed.connect(self._on_command_executed)
            worker.parse_started.connect(lambda t=text: self._on_parse_started(t))
            worker.parse_done.connect(self._on_parse_done)
            worker.finished.connect(lambda w=worker: self._reap_inject(w))
            self._inject_workers.add(worker)
            worker.start()
        else:
            if self._recorder.is_recording:
                self._status.setText("Listening...")
                self._osd.set_partial(text, write_mode=self._write_mode)
            else:
                self._status.setText("Idle.")
                self._osd.set_committed(text, transient_ms=1500)

        if self._recorder.is_recording:
            self._status.setText("Listening...")
        else:
            self._status.setText("Idle.")

    def _on_inject_done(self, original_text: str) -> None:
        # Post-inject: status line clears, prompt stays bright.
        # BUT: if the worker just emitted pending_action (and the main
        # thread set self._pending_action), don't override the
        # set_pending(...) state with set_committed(...). Pending OSD
        # already shows the right hint + cursor.
        if self._pending_action is None:
            if self._recorder.is_recording:
                self._osd.set_committed(original_text)
            else:
                self._osd.set_committed(original_text, transient_ms=1500)

        # Auto-stop if a command-style action just ran. Deferred so the
        # OSD's post-execution frame renders before recording stops.
        if self._auto_stop_pending and self._auto_stop_after_action:
            self._auto_stop_pending = False
            if self._recorder.is_recording:
                QTimer.singleShot(150, self._auto_stop)

    def _on_parse_started(self, prompt: str) -> None:
        """LLM parse is starting (only fires for the slow LLM parser)."""
        if self._injector and self._injector.is_slow_parser:
            self._osd.set_thinking(prompt)

    def _on_parse_done(self) -> None:
        """LLM parse finished. We don't have the ops at this point (worker
        keeps them locally) — just transition to a generic 'executing'
        status. The real action description would need an extra signal."""
        if self._injector and self._injector.is_slow_parser:
            self._osd.set_executing(self._osd._state.prompt or "")

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
        print(f"[korder] pending action set: {action_name!r}", flush=True)
        self._pending_action = action_name
        self._pending_action_time = time.time()
        action = get_action(action_name)
        param_label = ""
        if action and action.parameters:
            param_label = next(iter(action.parameters.keys()))
        prompt_so_far = self._osd._state.prompt or action_name
        hint = f"say the {param_label}…" if param_label else ""
        self._osd.set_pending(prompt_so_far, hint)
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
        # Run on a worker thread so execution doesn't block UI.
        self._osd.set_executing(param_text, action_name)
        worker = _InjectWorker(
            self._injector, "", param_text, self._write_mode,
            prebuilt_ops=[op],
        )
        worker.done.connect(self._on_inject_done)
        worker.failed.connect(self._on_inject_failed)
        worker.mode_changed.connect(self._on_mode_changed)
        worker.pending_action.connect(self._on_pending_action)
        worker.command_executed.connect(self._on_command_executed)
        worker.parse_started.connect(lambda t=param_text: self._on_parse_started(t))
        worker.parse_done.connect(self._on_parse_done)
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
            print(
                f"[korder] draining queue: {next_text!r} (pending={self._pending_action!r})",
                flush=True,
            )
            self._process_commit(next_text)

    def _on_fail(self, msg: str) -> None:
        self._status.setText(f"Transcription failed: {msg}")


def _split_at_locked_prefix(prev: str, curr: str) -> tuple[str, str]:
    """Split ``curr`` into (locked, flux) at the longest word-aligned
    common prefix with ``prev``.

    "Locked" means: same characters appear in both, AND the boundary lands
    on a word edge in *both* prev and curr — so we never split a word in
    half. Whisper's incremental output sometimes ends a partial mid-word
    ("what is th"), then revises it ("what is the time"); we keep "th"
    in the flux zone in that case rather than locking it.

    Returns ``("", curr)`` when there's no usable common prefix (Whisper
    revised the whole thing).
    """
    if not prev:
        return "", curr
    n = min(len(prev), len(curr))
    common_len = 0
    while common_len < n and prev[common_len] == curr[common_len]:
        common_len += 1
    if common_len == 0:
        return "", curr

    def _is_boundary(s: str, p: int) -> bool:
        # End-of-string or a whitespace at position p.
        return p >= len(s) or s[p] == " "

    p = common_len
    while p > 0 and not (_is_boundary(prev, p) and _is_boundary(curr, p)):
        p -= 1
    if p == 0:
        return "", curr
    locked = curr[:p].rstrip()
    flux = curr[len(locked):]
    return locked, flux


def _normalize_for_compare(text: str) -> str:
    """Lowercase + strip non-word chars so 'Hello, world' == 'hello world.'"""
    return re.sub(r"[^\w\s]", "", text.lower()).strip()
