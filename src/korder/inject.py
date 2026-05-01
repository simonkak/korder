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

    def __init__(
        self,
        paste_mode: str = "auto",
        op_parser=None,
        op_parser_is_warm=None,
        op_parser_warm_up=None,
        op_parser_last_response=None,
    ):
        if shutil.which("ydotool") is None:
            raise InjectError(
                "ydotool not found in PATH. Install it (e.g. `sudo pacman -S ydotool`) "
                "and run scripts/setup-uinput.sh once."
            )
        self.paste_mode = paste_mode
        self._has_wl_copy = shutil.which("wl-copy") is not None
        self._op_parser = op_parser or _split_into_ops
        self._op_parser_is_warm = op_parser_is_warm
        self._op_parser_warm_up = op_parser_warm_up
        # Optional callable -> str. Called AFTER parse_ops to read the
        # LLM's natural-language response from the same parse call.
        # Used for confirmation prompts today; future home for
        # conversational replies and TTS-spoken progress.
        self._op_parser_last_response = op_parser_last_response
        self._lock = threading.Lock()
        self.is_slow_parser = op_parser is not None

    def last_op_parser_response(self) -> str:
        """Return the LLM's response for the most recent parse_ops call,
        or empty string if none. Caller is responsible for calling this
        immediately after parse_ops to avoid races with subsequent
        parses."""
        if self._op_parser_last_response is None:
            return ""
        try:
            return self._op_parser_last_response() or ""
        except Exception:
            return ""

    def warm_up_op_parser(self) -> None:
        """Opportunistic preload (fire-and-forget). Called when the mic
        opens so the model loads in parallel with the user's speech;
        keeps the post-Whisper LLM call off the cold-start path. No-op
        for parsers without a load step (regex)."""
        if self._op_parser_warm_up is None:
            return
        try:
            self._op_parser_warm_up()
        except Exception:
            pass

    def is_op_parser_warm(self) -> bool:
        """Returns True if the parser is ready for a fast call. False
        means the next call is expected to pay a cold-start cost (e.g.
        ollama needs to load the model into VRAM). Used by the UI to
        decide between a 'Loading' and a 'Thinking' state. Defaults to
        True for parsers without a load step (regex)."""
        if self._op_parser_is_warm is None:
            return True
        try:
            return bool(self._op_parser_is_warm())
        except Exception:
            return True

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
            elif kind == "system_volume":
                self._run_system_volume(op[1])
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

    def _run_system_volume(self, payload) -> None:
        """Adjust the default audio sink directly via wpctl. Replaces the
        old KEY_VOLUMEUP/DOWN/MUTE keycode path: routing through KDE's
        media-key handler raced with the ducker (the keycode arrived at
        plasma-pa before our wpctl-set 'restore' value had propagated
        there), so 'louder' would land on the still-ducked level. Going
        through wpctl matches the ducker's own write path — same IPC
        channel, no synchronization surprises.

        Payload is (direction, step_pct). step_pct is an int percent of
        full scale; ignored for mute_toggle. Action layer is responsible
        for clamping to a sane range."""
        try:
            direction, step_pct = payload
        except (TypeError, ValueError):
            print(f"[korder] system_volume: bad payload {payload!r}", flush=True)
            return
        if direction == "up":
            args = ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{step_pct}%+"]
        elif direction == "down":
            args = ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{step_pct}%-"]
        elif direction == "mute_toggle":
            args = ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"]
        else:
            print(f"[korder] system_volume: unknown direction {direction!r}", flush=True)
            return
        try:
            subprocess.run(args, check=False, capture_output=True, timeout=2.0)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"[korder] system_volume {direction} failed: {e}", flush=True)

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


def make_backend(
    paste_mode: str = "auto",
    op_parser=None,
    op_parser_is_warm=None,
    op_parser_warm_up=None,
    op_parser_last_response=None,
) -> YdotoolBackend:
    return YdotoolBackend(
        paste_mode=paste_mode,
        op_parser=op_parser,
        op_parser_is_warm=op_parser_is_warm,
        op_parser_warm_up=op_parser_warm_up,
        op_parser_last_response=op_parser_last_response,
    )
