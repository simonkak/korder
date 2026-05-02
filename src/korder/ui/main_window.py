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
from korder.ui.i18n import t
from korder.ui.progress import progress_signal


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
    loading_started = Signal()  # LLM model not resident — cold load + parse
    parse_done = Signal()  # parse finished, executing now
    cancel_requested = Signal()  # user said 'cancel'/'nevermind' — abort it all
    parse_response = Signal(str)  # LLM's free-form reply from the parse JSON

    def __init__(
        self,
        injector: YdotoolBackend,
        payload: str,
        original: str,
        initial_write_mode: bool,
        prebuilt_ops: list | None = None,
        ducker: VolumeDucker | None = None,
    ):
        super().__init__()
        self._injector = injector
        self._payload = payload
        self._original = original
        self._write_mode = initial_write_mode
        self._prebuilt_ops = prebuilt_ops
        self._ducker = ducker

    def run(self) -> None:
        try:
            if self._prebuilt_ops is not None:
                ops = self._prebuilt_ops
            else:
                # Pre-flight: ask the parser whether the model is warm.
                # When false (LLM only — regex always reports warm), the
                # next call pays a cold-load. Surface that to the UI as
                # a separate Loading state so the user understands the
                # extra second isn't reasoning, it's paging the model in.
                if self._injector.is_slow_parser and not self._injector.is_op_parser_warm():
                    print(
                        "[korder] parse: model not resident — cold start, showing Loading",
                        flush=True, file=sys.stderr,
                    )
                    self.loading_started.emit()
                else:
                    self.parse_started.emit()
                t0 = time.perf_counter()
                ops = self._injector.parse_ops(self._payload)
                # Snapshot the LLM's response field BEFORE any other
                # parse can race with us. Empty string when the LLM
                # didn't include a response or we're on the regex path.
                response = ""
                last_response_fn = getattr(
                    self._injector, "last_op_parser_response", None
                )
                if callable(last_response_fn):
                    response = last_response_fn() or ""
                if response:
                    self.parse_response.emit(response)
                if self._injector.is_slow_parser:
                    print(
                        f"[korder] parse: completed in {(time.perf_counter()-t0)*1000:.0f} ms",
                        flush=True, file=sys.stderr,
                    )
                self.parse_done.emit()
            # Cancel takes priority over everything else in the batch.
            # When the user said 'cancel' / 'nevermind', drop any pending
            # text and any other actions in the same utterance — the
            # whole transcript is discarded. parse_done already fired so
            # the OSD has cleaned up its Thinking state; the main thread
            # handles the rest via cancel_requested.
            if any(op[0] == "cancel" for op in ops):
                print(
                    "[korder] cancel_session: aborting batch, signalling main thread",
                    flush=True, file=sys.stderr,
                )
                self.cancel_requested.emit()
                return
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
            # If the user asked to change volume, the duck-while-listening
            # snapshot is no longer authoritative — they've issued an
            # explicit volume command. Restore now so the wpctl step
            # lands on the user's true level (otherwise "louder" goes
            # from 30% ducked to 35% and the post-action restore-to-50%
            # silently undoes the increase).
            if self._ducker is not None and any(
                op[0] == "system_volume" for op in filtered
            ):
                self._ducker.restore()
            self._injector.execute_ops(filtered)
            if had_command_action:
                self.command_executed.emit()
            self.done.emit(self._original)
        except Exception as e:
            self.failed.emit(str(e))


class MainWindow(QMainWindow):
    # Tray and any other observer can listen here to swap state — values:
    # 'idle', 'wake_listening', 'dictating'. Emitted on every transition
    # plus once at startup so observers don't miss the initial state.
    tray_state_changed = Signal(str)

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
        wake_detector=None,
        wake_idle_timeout_s: float = 5.0,
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
        # Wake-word activation (issue #1). Optional — None means hotkey-only.
        self._wake_detector = wake_detector
        self._wake_idle_timeout_s = float(wake_idle_timeout_s)
        # LLM's free-form response from the most recent parse, if any.
        # Captured by the inject worker right after parse_ops returns
        # (so it can't be raced by a later parse) and emitted to the
        # main thread for use as a pending-action hint, future TTS,
        # etc. Cleared on each transition out of pending.
        self._last_llm_response: str = ""
        # True for the dictation session that began via a wake event.
        # Resets to False on every dictation end. Used to gate the
        # idle-timeout timer that returns accidental wakes to wake-mode.
        self._dictation_via_wake = False
        self._wake_idle_timer = QTimer(self)
        self._wake_idle_timer.setSingleShot(True)
        self._wake_idle_timer.timeout.connect(self._on_wake_idle_timeout)
        if self._wake_detector is not None:
            self._wake_detector.detected.connect(self._on_wake_detected)
            self._wake_detector.error.connect(self._on_wake_error)
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

        # Cross-thread progress narration from action executors. Lets
        # actions like spotify_search push "Searching for X" / "Found Y" /
        # "Playing Y" into the OSD center text while we're in Executing.
        progress_signal().connect(self._on_executing_progress)

        central = QWidget()
        layout = QVBoxLayout(central)

        self._status = QLabel(t("status_idle"))
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

    # ---- wake-word activation (issue #1) ---------------------------------

    def is_wake_listening(self) -> bool:
        return self._wake_detector is not None and self._wake_detector.is_running

    def start_wake_listening(self) -> None:
        """Begin always-on wake-word listening. The detector subscribes
        to MicRecorder, which keeps the audio stream open until the
        last subscriber detaches. No-op if no detector is configured
        or it's already running."""
        if self._wake_detector is None or self._wake_detector.is_running:
            return
        try:
            self._wake_detector.start()
        except Exception as e:
            print(f"[korder] wake: failed to start: {e}", flush=True, file=sys.stderr)
            self.statusBar().showMessage(f"Wake-word: {e}", 8000)
            return
        self._emit_tray_state()

    def stop_wake_listening(self) -> None:
        """Disable wake-word listening. Closes the audio stream if no
        dictation is in progress."""
        if self._wake_detector is None or not self._wake_detector.is_running:
            return
        self._wake_detector.stop()
        self._emit_tray_state()

    def toggle_wake_listening(self) -> None:
        if self.is_wake_listening():
            self.stop_wake_listening()
        else:
            self.start_wake_listening()

    def _on_wake_detected(self) -> None:
        """Wake phrase fired. Begin a dictation session as if the user
        had pressed the hotkey. If a dictation is already in flight
        (e.g. user spoke their wake phrase mid-utterance), ignore."""
        if self._recorder.is_recording:
            return
        self._dictation_via_wake = True
        self._start_recording()
        self._sync_button()

    def _on_wake_error(self, msg: str) -> None:
        print(f"[korder] wake: {msg}", flush=True, file=sys.stderr)

    def _on_wake_idle_timeout(self) -> None:
        """No speech arrived within wake_idle_timeout_s of a wake fire.
        Probably an accidental wake — cancel and return to wake-listening
        so the OSD doesn't sit open on a false positive."""
        if not self._recorder.is_recording:
            return
        print(
            "[korder] wake: dictation idle-timeout — canceling, returning to wake-listen",
            flush=True, file=sys.stderr,
        )
        self.cancel_recording()

    def _emit_tray_state(self) -> None:
        """Compute the current observable state and emit so the tray can
        swap its icon/tooltip. Called from every transition."""
        if self._recorder.is_recording:
            state = "dictating"
        elif self.is_wake_listening():
            state = "wake_listening"
        else:
            state = "idle"
        self.tray_state_changed.emit(state)

    def cancel_recording(self) -> None:
        """Abort the current recording without transcribing or injecting.

        Called from the IPC server when the user binds a global hotkey
        (typically Esc) to ``korder cancel``. Discards the audio buffer,
        stops timers, hides the OSD. No-op if not currently recording.
        """
        if not self._recorder.is_recording:
            return
        self._partial_timer.stop()
        self._osd_throttle_timer.stop()
        self._wake_idle_timer.stop()
        self._dictation_via_wake = False
        self._pending_partial_text = None
        # Drain and drop the recorder's buffer (sounddevice closes its stream
        # in stop()); we ignore the returned array.
        try:
            self._recorder.stop()
        except Exception:
            pass
        self._end_dictation_lifecycle()
        self._status.setText(t("status_cancelled"))
        self._osd.hide_after(150)
        self._sync_button()
        self._emit_tray_state()

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
            self._dictation_via_wake = False
            return
        self._begin_dictation_lifecycle()
        self._status.setText(t("status_listening"))
        self._osd.set_listening(write_mode=self._write_mode)
        # Opportunistic preload — kick the model load now (fire-and-
        # forget) so it runs in parallel with the user's speech +
        # whisper. Hides the ~3s ollama cold-start behind dictation
        # latency on the LLM path; no-op on regex.
        if self._injector is not None:
            self._injector.warm_up_op_parser()
        self._partial_in_flight = False
        self._committed_samples = 0
        self._last_partial_norm = ""
        self._stability_count = 0
        self._reset_partial_render_state()
        self._partial_timer.start()
        # Arm the wake idle-timeout only when this dictation began via
        # a wake event. Hotkey/tray invocations stay open as long as
        # the user wants — we don't want the timer slamming a manual
        # session shut.
        if self._dictation_via_wake and self._wake_idle_timeout_s > 0:
            self._wake_idle_timer.start(int(self._wake_idle_timeout_s * 1000))
        self._emit_tray_state()

    def _begin_dictation_lifecycle(self) -> None:
        """Resources to acquire for the duration of a dictation session,
        as opposed to merely 'mic is open'. Today the two events
        coincide; once wake-word activation lands, the mic may already
        be open (for the wake listener) when dictation begins, and the
        duck has to fire on dictation start, not mic open."""
        self._ducker.duck()

    def _end_dictation_lifecycle(self) -> None:
        """Mirror of _begin_dictation_lifecycle. Called from every exit
        path (auto-stop, manual stop, cancel, error) so the duck snapshot
        is always restored exactly once per dictation."""
        try:
            self._ducker.restore()
        except Exception:
            pass

    def _stop_recording(self, *, transcribe_tail: bool = True) -> None:
        """Close the mic and (by default) transcribe any audio captured
        since the last commit as one final 'tail' commit.

        Set ``transcribe_tail=False`` when the stop is triggered by the
        system (auto-stop after a command, idle-timeout, etc.) rather
        than by the user. In those cases the user wasn't meant to be
        speaking — anything on the buffer is noise or stray follow-up
        the user didn't intend as a new command, and forcing a final
        Whisper pass on it produces spurious transcriptions like the
        "I teraz co myślisz?" the user reported."""
        if not self._recorder.is_recording:
            return
        self._partial_timer.stop()
        self._osd_throttle_timer.stop()
        self._wake_idle_timer.stop()
        self._dictation_via_wake = False
        self._pending_partial_text = None
        full = self._recorder.stop()
        # Restore volume the moment the mic closes, regardless of whether
        # transcription proceeds or short-circuits below. Whisper runs
        # off-thread; we don't want playback held down for the duration.
        self._end_dictation_lifecycle()
        self._emit_tray_state()
        if not transcribe_tail:
            self._status.setText(t("status_idle"))
            self._osd.hide_after(300)
            return
        sr = self._recorder.sample_rate
        remaining = full[self._committed_samples:]
        if remaining.size < int(0.2 * sr) or not self._detector.has_speech(remaining):
            self._status.setText(t("status_idle"))
            self._osd.hide_after(300)
            return
        self._status.setText(t("status_transcribing"))
        self._osd.set_thinking(self._osd._state.prompt or "", t("transcribing"))
        self._submit_transcribe(remaining, kind="commit")

    # OSD states during which Whisper should actively transcribe new
    # audio. The "system is busy" states — loading / thinking /
    # executing — are excluded so we don't waste CPU on noise the user
    # isn't meant to be producing and don't race partials against OSD
    # transitions. "committed" stays IN the active set on purpose:
    # it's how the OSD looks after a pure-text commit (no action
    # fired) when the user is still recording and may continue
    # dictating; without this, post-text dictation would freeze with
    # no way back to listening short of toggling the hotkey. For the
    # command-flow committed (auto-stop scheduled in ~150 ms),
    # _stop_recording's transcribe_tail=False drops anything Whisper
    # captures during the brief window, so allowing it here doesn't
    # leak spurious commits.
    _WHISPER_ACTIVE_STATES = frozenset({"listening", "pending", "committed"})

    def _on_partial_tick(self) -> None:
        if not self._recorder.is_recording:
            return
        # Gate on OSD state — see _WHISPER_ACTIVE_STATES above.
        if self._osd._state.stateKind not in self._WHISPER_ACTIVE_STATES:
            return
        sr = self._recorder.sample_rate
        full = self._recorder.snapshot()
        new = full[self._committed_samples:]
        if new.size < int(0.3 * sr):
            return

        # Once any real speech is detected, disarm the wake idle-timeout
        # so the user can take their time finishing the utterance. This
        # only matters for sessions that started via wake; the timer
        # isn't armed otherwise.
        if self._wake_idle_timer.isActive() and self._detector.has_speech(
            new, min_speech_ms=self.MIN_SPEECH_FOR_PARTIAL_MS
        ):
            self._wake_idle_timer.stop()

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
        self._end_dictation_lifecycle()
        for w in list(self._workers):
            w.wait(2000)
        for w in list(self._inject_workers):
            w.wait(2000)

    def _on_commit_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            if not self._recorder.is_recording:
                self._status.setText(t("status_idle"))
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
            worker = _InjectWorker(
                self._injector, payload, text, self._write_mode,
                ducker=self._ducker,
            )
            worker.done.connect(self._on_inject_done)
            worker.failed.connect(self._on_inject_failed)
            worker.mode_changed.connect(self._on_mode_changed)
            worker.pending_action.connect(self._on_pending_action)
            worker.command_executed.connect(self._on_command_executed)
            worker.parse_started.connect(lambda t=text: self._on_parse_started(t))
            worker.loading_started.connect(lambda t=text: self._on_loading_started(t))
            worker.parse_done.connect(self._on_parse_done)
            worker.cancel_requested.connect(self._on_cancel_requested)
            worker.parse_response.connect(self._on_parse_response)
            worker.finished.connect(lambda w=worker: self._reap_inject(w))
            self._inject_workers.add(worker)
            worker.start()
        else:
            if self._recorder.is_recording:
                self._status.setText(t("status_listening"))
                self._osd.set_partial(text, write_mode=self._write_mode)
            else:
                self._status.setText(t("status_idle"))
                self._osd.set_committed(text, transient_ms=1500)

        if self._recorder.is_recording:
            self._status.setText(t("status_listening"))
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

        # Capture "did a command fire this round?" before mutating the
        # flag below. _auto_stop_pending is True iff command_executed
        # fired during this inject worker's run.
        command_fired = self._auto_stop_pending
        self._auto_stop_pending = False  # always reset, never leak

        # Auto-stop if a command-style action just ran AND auto-stop is
        # configured. Deferred so the OSD's post-execution frame renders
        # before recording stops.
        if command_fired and self._auto_stop_after_action:
            if self._recorder.is_recording:
                QTimer.singleShot(150, self._auto_stop)
            return

        # Conversational-answer path: the user asked a question with no
        # registered action match, and Gemma populated `response` with
        # a free-form reply (math, definitions, translation, general
        # knowledge). Show the answer in the OSD instead of falling
        # through to the "didn't get that" reset, then transition back
        # to listening so the user can ask a follow-up.
        if (
            not command_fired
            and self._pending_action is None
            and self._last_llm_response
            and self._recorder.is_recording
        ):
            answer = self._last_llm_response
            self._last_llm_response = ""
            QTimer.singleShot(120, lambda a=answer: self._show_conversational_answer(a))
            return

        # Pure-dictation path: the LLM produced no command actions this
        # round (or auto-stop is off). If the recorder is still open and
        # there's no pending parameter expected, give the user a fresh
        # listening state with a "didn't get that" hint after a brief
        # pause — long enough that a quick follow-up utterance starts
        # transcribing first (set_partial transitions us to listening
        # naturally and the reset becomes a no-op), short enough that a
        # silent user sees the hint instead of a stale committed frame.
        if (
            not command_fired
            and self._pending_action is None
            and self._recorder.is_recording
        ):
            QTimer.singleShot(700, self._reset_to_listening_after_miss)

    def _show_conversational_answer(self, answer: str) -> None:
        """Display Gemma's free-form reply for a no-action conversational
        query (math, definition, translation, general knowledge).
        Reuses the executing-progress visual treatment — italic, accent
        color — so the answer reads as system feedback distinct from
        the user's own dictated text. After a read window proportional
        to the answer's length, transition back to listening so the
        user can ask a follow-up. No-op if the recorder closed or
        another state took over in the interim."""
        if not self._recorder.is_recording:
            return
        if self._pending_action is not None:
            return
        print(f"[korder] conversational answer: {answer!r}", flush=True)
        self._osd.set_executing_progress(answer)
        # Read window: ~50 ms per character + 1.5 s minimum + 7 s
        # ceiling. Long enough for Polish multi-clause sentences, short
        # enough that an absent user doesn't sit on a stale answer.
        read_ms = max(1500, min(7000, 1500 + 50 * len(answer)))
        QTimer.singleShot(read_ms, self._reset_to_listening_after_answer)

    def _reset_to_listening_after_answer(self) -> None:
        """Mirror of _reset_to_listening_after_miss but for the path
        where a conversational answer was just displayed. Same
        end-state — fresh listening with placeholder — but expects to
        be called from 'executing' rather than 'committed'."""
        if not self._recorder.is_recording:
            return
        if self._pending_action is not None:
            return
        if self._osd._state.stateKind != "executing":
            return  # user started a new utterance — let that flow win
        # Skip past any audio captured while the user was reading.
        self._committed_samples = self._recorder.snapshot().shape[0]
        self._reset_partial_render_state()
        self._last_partial_norm = ""
        self._stability_count = 0
        self._osd.set_listening(write_mode=self._write_mode)

    def _reset_to_listening_after_miss(self) -> None:
        """Transition from 'committed' to a fresh 'listening' with a
        'didn't get that' placeholder, so the user sees the LLM didn't
        recognize a command and the recorder is ready for another try.
        No-op if the user already started talking (set_partial moved us
        to listening), entered pending mode, or stopped recording in
        the interim."""
        if not self._recorder.is_recording:
            return
        if self._pending_action is not None:
            return
        if self._osd._state.stateKind != "committed":
            return
        # Skip past the audio captured during Thinking/Executing/Done
        # so the next partial-tick starts fresh — anything spoken
        # during the system-busy window was unintended (the LLM just
        # told us this round produced no command).
        self._committed_samples = self._recorder.snapshot().shape[0]
        self._reset_partial_render_state()
        self._last_partial_norm = ""
        self._stability_count = 0
        self._osd.set_listening(
            write_mode=self._write_mode,
            placeholder_key="didnt_get_that",
        )

    def _on_parse_started(self, prompt: str) -> None:
        """LLM parse is starting (only fires for the slow LLM parser)."""
        if self._injector and self._injector.is_slow_parser:
            self._osd.set_thinking(prompt)

    def _on_loading_started(self, prompt: str) -> None:
        """Cold-start path: ollama needs to page the model into VRAM
        before it can reason. Fires instead of parse_started when the
        model wasn't resident at request time. The same parse_done
        signal then transitions to Executing once the call returns."""
        if self._injector and self._injector.is_slow_parser:
            self._osd.set_loading(prompt)

    def _on_parse_done(self) -> None:
        """LLM parse finished. We don't have the ops at this point (worker
        keeps them locally) — just transition to a generic 'executing'
        status. The real action description would need an extra signal."""
        if self._injector and self._injector.is_slow_parser:
            self._osd.set_executing(self._osd._state.prompt or "")

    def _on_parse_response(self, response: str) -> None:
        """The LLM populated the JSON `response` field. Stash it so
        downstream slots (currently only _on_pending_action) can use it
        as a contextual prompt. Cleared on the next round's
        _on_pending_action and on cancel/error paths so a stale
        response doesn't leak into a different turn."""
        self._last_llm_response = response or ""

    def _on_cancel_requested(self) -> None:
        """User said 'cancel' / 'nevermind' mid-recording. The worker
        already aborted the inject batch (no text typed, no actions
        fired). Tear down the recorder + OSD the same way the ESC-key
        cancel path does, and clear any pending-action / auto-stop
        bookkeeping so the next session starts fresh."""
        print("[korder] cancel_session: aborting recording on user request", flush=True)
        self._auto_stop_pending = False
        self._pending_action = None
        self.cancel_recording()

    def _on_inject_failed(self, msg: str) -> None:
        self.statusBar().showMessage(f"Inject failed: {msg}", 8000)
        self._auto_stop_pending = False

    def _on_command_executed(self) -> None:
        """Worker reports a non-text, non-mode-toggle action ran. Flag set
        so _on_inject_done can stop recording after the UI update lands."""
        self._auto_stop_pending = True

    def _on_executing_progress(self, text: str) -> None:
        """Narration from a running action (Spotify search, etc.). Only
        applied when we're actually in Executing — otherwise a stray
        progress event from a previous command would clobber whatever
        state we've moved on to."""
        if self._osd._state.stateKind != "executing":
            return
        if not text:
            return
        self._osd.set_executing_progress(text)

    def _auto_stop(self) -> None:
        """Quietly stop recording — used after a command finishes.
        transcribe_tail=False so the buffer captured during
        Thinking/Executing (which the user wasn't meant to dictate
        into) doesn't get flushed through Whisper as a spurious final
        commit when the mic closes."""
        if self._recorder.is_recording:
            print("[korder] auto-stop after command", flush=True)
            self._stop_recording(transcribe_tail=False)
            self._sync_button()

    def _on_mode_changed(self, write_mode: bool) -> None:
        """Worker reports the user toggled write mode mid-commit."""
        self._write_mode = write_mode
        self.statusBar().showMessage(
            t("status_write_mode_on") if write_mode else t("status_write_mode_off"),
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
        # Hint resolution chain, highest-priority first:
        #   1. LLM-supplied `response` from the just-finished parse —
        #      Gemma writes a contextual confirmation in the user's
        #      input language as part of the same JSON it returns
        #      actions in. Free of extra round-trips.
        #   2. Action-specific pending_prompt_<name> i18n key — static
        #      hand-written fallback when the LLM omitted `response`
        #      (model variance) or we're on the regex path (no LLM).
        #   3. Per-parameter say_the_param_<label> — generic param
        #      extraction prompt (Spotify search query, etc.).
        #   4. The bare pending_param_hint — last-resort placeholder.
        # i18n's t() returns the key itself on miss, so a
        # key-equals-result comparison detects the fall-through.
        hint: str = self._last_llm_response
        # Consume the response so a subsequent non-pending parse can't
        # leak this turn's text into a later turn's hint.
        self._last_llm_response = ""
        if not hint:
            action_key = f"pending_prompt_{action_name}"
            action_hint = t(action_key)
            if action_hint != action_key:
                hint = action_hint
            elif param_label:
                specific_key = f"say_the_param_{param_label}"
                specific = t(specific_key)
                hint = specific if specific != specific_key else t("pending_param_hint")
            else:
                hint = t("pending_param_hint")
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
            prebuilt_ops=[op], ducker=self._ducker,
        )
        worker.done.connect(self._on_inject_done)
        worker.failed.connect(self._on_inject_failed)
        worker.mode_changed.connect(self._on_mode_changed)
        worker.pending_action.connect(self._on_pending_action)
        worker.command_executed.connect(self._on_command_executed)
        worker.parse_started.connect(lambda t=param_text: self._on_parse_started(t))
        worker.parse_done.connect(self._on_parse_done)
        worker.cancel_requested.connect(self._on_cancel_requested)
        worker.parse_response.connect(self._on_parse_response)
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
