"""Lower system playback volume while Korder is listening.

Voice transcription accuracy plummets when the mic picks up speakers
playing back at high volume — Whisper hears two streams interleaved and
guesses badly. This module ducks the default PipeWire sink for the
duration of a recording session, then restores the original level.

Wire control through `wpctl` (PipeWire's CLI). It's part of the
wireplumber package and present on every PipeWire system; if missing,
the ducker silently no-ops rather than blowing up the recording.

Lifecycle (driven by MainWindow):
    ducker.duck()       on _start_recording
    ducker.restore()    on _stop_recording (every exit path)
    ducker.restore()    via atexit, as a crash-safety net

Idempotency: duck() while already ducked is a no-op (preserves the
*original* saved level, not the already-lowered one). restore() while
not ducked is a no-op.
"""
from __future__ import annotations
import atexit
import re
import subprocess
import threading


_VOLUME_RE = re.compile(r"Volume:\s+([\d.]+)")
_WPCTL_TIMEOUT_S = 2.0
_DEFAULT_SINK = "@DEFAULT_AUDIO_SINK@"


class VolumeDucker:
    def __init__(self, enabled: bool, target_pct: int):
        self._enabled = enabled
        # Clamp to [0, 100] then convert to wpctl's 0.00–1.00 scale.
        self._target = max(0, min(100, int(target_pct))) / 100.0
        # None = not currently ducked. Set to original level on duck(),
        # cleared on restore(). We never overwrite a non-None value so
        # that a duplicate duck() call doesn't replace the original
        # level with the already-lowered one.
        self._saved: float | None = None
        # restore() can be invoked from the inject worker thread (when a
        # volume-altering action is about to fire and needs the original
        # level back) and from the main thread (auto-stop after-action).
        # The lock makes the read-modify-write of _saved + wpctl call
        # safely interleavable.
        self._lock = threading.Lock()
        if self._enabled:
            # Crash safety: if the process exits while ducked (uncaught
            # exception, SIGTERM via tray-quit, …), put the volume back.
            atexit.register(self._safe_restore)

    def duck(self) -> None:
        with self._lock:
            if not self._enabled or self._saved is not None:
                return
            current = self._read_volume()
            if current is None:
                return
            # Don't bother lowering if we're already at-or-below the target —
            # would just create a confusing restore-up-to-target later.
            if current <= self._target:
                return
            if self._set_volume(self._target):
                self._saved = current
                print(
                    f"[korder] ducker: {current:.2f} → {self._target:.2f}",
                    flush=True,
                )

    def restore(self) -> None:
        with self._lock:
            if self._saved is None:
                return
            if self._set_volume(self._saved):
                print(f"[korder] ducker: restored {self._saved:.2f}", flush=True)
            # Clear saved state regardless — a failed restore call shouldn't
            # leave us pinned in "ducked" forever; the next duck() can re-read
            # whatever the user left things at.
            self._saved = None

    def _safe_restore(self) -> None:
        try:
            self.restore()
        except Exception:
            pass

    def _read_volume(self) -> float | None:
        try:
            result = subprocess.run(
                ["wpctl", "get-volume", _DEFAULT_SINK],
                capture_output=True,
                text=True,
                timeout=_WPCTL_TIMEOUT_S,
                check=True,
            )
        except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
            print(f"[korder] ducker: get-volume failed: {e}", flush=True)
            return None
        match = _VOLUME_RE.search(result.stdout)
        if match is None:
            return None
        try:
            return float(match.group(1))
        except ValueError:
            return None

    def _set_volume(self, level: float) -> bool:
        try:
            subprocess.run(
                ["wpctl", "set-volume", _DEFAULT_SINK, f"{level:.2f}"],
                capture_output=True,
                timeout=_WPCTL_TIMEOUT_S,
                check=True,
            )
            return True
        except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
            print(f"[korder] ducker: set-volume failed: {e}", flush=True)
            return False
