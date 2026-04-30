"""ydotool/wl-clipboard injection backend.

This module is now thin — actions are registered in korder.actions, the
op-list builder lives in korder.actions.parser, and YdotoolBackend just
executes whatever op tuples come back. Backward compatibility shims
(_split_into_ops re-export, NAMED_SHORTCUTS) are kept so the rest of
the codebase and any external scripts keep working.
"""
from __future__ import annotations
import shutil
import subprocess
import threading
import time

from korder.actions.codes import KEY_LCTRL, KEY_V
from korder.actions.parser import split_into_ops as _split_into_ops  # noqa: F401  (re-export)


class InjectError(RuntimeError):
    pass


# Legacy keycode aliases retained for any external imports.
_KEY_LCTRL = KEY_LCTRL
_KEY_V = KEY_V


# Legacy alias preserved so any callers that imported NAMED_SHORTCUTS keep
# working. New code should query the action registry directly.
def _legacy_named_shortcuts() -> dict[str, list[int]]:
    from korder.actions.base import all_actions
    out: dict[str, list[int]] = {}
    for action in all_actions():
        op = action.op_factory({})
        if op is None:
            # Parameterized actions return None when params are missing —
            # they're not shortcuts in the legacy sense.
            continue
        if op[0] == "combo" and isinstance(op[1], list):
            out[action.name] = op[1]
    return out


NAMED_SHORTCUTS = _legacy_named_shortcuts()


class YdotoolBackend:
    """Synthesize keystrokes via ydotool (uinput). For non-ASCII text on Wayland,
    routes through the clipboard with wl-copy + Ctrl+V because ydotool's keycode
    synthesis cannot produce characters that aren't single keys in the active keymap."""

    PASTE_AUTO = "auto"
    PASTE_ALWAYS = "always"
    PASTE_NEVER = "never"

    def __init__(self, paste_mode: str = "auto", op_parser=None):
        if shutil.which("ydotool") is None:
            raise InjectError(
                "ydotool not found in PATH. Install it (e.g. `sudo pacman -S ydotool`) "
                "and run scripts/setup-uinput.sh once."
            )
        self.paste_mode = paste_mode
        self._has_wl_copy = shutil.which("wl-copy") is not None
        self._op_parser = op_parser or _split_into_ops
        self._lock = threading.Lock()
        self.is_slow_parser = op_parser is not None

    def parse_ops(self, text: str) -> list[tuple]:
        """Parse text into op tuples WITHOUT executing. Lets the caller
        inspect/filter ops (e.g., apply write-mode gating) before running."""
        if not text:
            return []
        return self._op_parser(text)

    def execute_ops(self, ops: list[tuple]) -> None:
        """Run a pre-built op list. Caller is responsible for any filtering
        (mode toggles, write-mode gating)."""
        if not ops:
            return
        with self._lock:
            self._execute_locked(ops)

    def type(self, text: str) -> None:
        """Convenience: parse_ops + execute_ops in one call. Used when no
        filtering is needed."""
        ops = self.parse_ops(text)
        self.execute_ops(ops)

    def _execute_locked(self, ops: list[tuple]) -> None:
        for i, op in enumerate(ops):
            kind = op[0]
            if kind == "text" or kind == "char":
                segment: str = op[1]
                if self._should_paste(segment):
                    self._paste(segment)
                else:
                    self._direct_type(segment)
            elif kind == "key":
                self._press_key(op[1])
            elif kind == "combo":
                self._press_combo(op[1])
            elif kind == "subprocess":
                self._run_subprocess(op[1])
            elif kind == "callable":
                self._run_callable(op[1])
            # write_mode ops are advisory — caller filters them before
            # passing to execute_ops; if any slip through, no-op.
            if i < len(ops) - 1:
                time.sleep(0.04)

    def _run_callable(self, fn) -> None:
        """Execute an arbitrary Python callable (typically a closure capturing
        whatever state the action needs — config, params, etc.). Errors are
        logged but not raised so a failing action doesn't kill the worker."""
        try:
            fn()
        except Exception as e:
            print(f"[korder] callable action failed: {e}", flush=True)

    def _run_subprocess(self, args: list[str]) -> None:
        """Issue a system command (volume, media, etc.) — fire and forget.
        Failure is non-fatal; users say 'next song' with nothing playing
        sometimes, which is harmless and shouldn't surface an error."""
        if not args:
            return
        try:
            subprocess.run(
                args,
                check=False,
                capture_output=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"[korder] subprocess action {args!r} failed: {e}", flush=True)

    def _should_paste(self, text: str) -> bool:
        if not self._has_wl_copy:
            return False
        if self.paste_mode == self.PASTE_ALWAYS:
            return True
        if self.paste_mode == self.PASTE_NEVER:
            return False
        return any(ord(c) > 127 for c in text)

    def _direct_type(self, text: str) -> None:
        try:
            subprocess.run(
                ["ydotool", "type", "--", text],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            hint = ""
            if "uinput" in stderr.lower() or "permission" in stderr.lower():
                hint = " (run scripts/setup-uinput.sh and start ydotoold)"
            raise InjectError(f"ydotool failed: {stderr or e}{hint}") from e

    def _paste(self, text: str) -> None:
        try:
            subprocess.run(
                ["wl-copy"],
                input=text,
                text=True,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except subprocess.CalledProcessError as e:
            raise InjectError(f"wl-copy failed: {e.returncode}") from e
        except subprocess.TimeoutExpired as e:
            raise InjectError(f"wl-copy timed out: {e}") from e
        time.sleep(0.04)
        self._press_combo([KEY_LCTRL, KEY_V])

    def _press_key(self, keycode: int) -> None:
        self._press_combo([keycode])

    def _press_combo(self, keycodes: list[int]) -> None:
        if not keycodes:
            return
        args = [f"{kc}:1" for kc in keycodes]
        args += [f"{kc}:0" for kc in reversed(keycodes)]
        try:
            subprocess.run(
                ["ydotool", "key", *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.CalledProcessError as e:
            raise InjectError(
                f"ydotool combo {keycodes} failed: {(e.stderr or '').strip() or e}"
            ) from e


def make_backend(paste_mode: str = "auto", op_parser=None) -> YdotoolBackend:
    return YdotoolBackend(paste_mode=paste_mode, op_parser=op_parser)
