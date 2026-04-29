from __future__ import annotations
import re
import shutil
import subprocess
import time


class InjectError(RuntimeError):
    pass


# Linux evdev keycodes.
_KEY_LCTRL = 29
_KEY_V = 47
_KEY_ENTER = 28
_KEY_TAB = 15
_KEY_ESCAPE = 1
_KEY_BACKSPACE = 14


# Inline action triggers: phrases in the transcript that get translated into
# special-key presses or character emissions instead of being typed verbatim.
# Keys require an explicit "press" prefix so prose like "she pressed enter"
# stays literal text. Char emissions like "new line" are unambiguous enough
# without a prefix.
_TRIGGERS: dict[str, tuple[str, object]] = {
    "press enter": ("key", _KEY_ENTER),
    "press return": ("key", _KEY_ENTER),
    "press tab": ("key", _KEY_TAB),
    "press escape": ("key", _KEY_ESCAPE),
    "press backspace": ("key", _KEY_BACKSPACE),
    "new line": ("char", "\n"),
    "new paragraph": ("char", "\n\n"),
}

_TRIGGER_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in sorted(_TRIGGERS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _split_into_ops(text: str) -> list[tuple]:
    """Split a transcript into a sequence of ('text', s) / ('key', code) /
    ('char', s) ops. Returns an empty list for empty input."""
    if not text:
        return []
    # Strip whitespace and trailing punctuation Whisper adds around the
    # trigger phrase (a trailing period after "Press Enter." for example).
    # Leading commas after a trigger likewise dropped.
    _PUNCT_AFTER = " \t.!?,;:"
    _PUNCT_BEFORE = " \t.!?;:"
    ops: list[tuple] = []
    last_end = 0
    for m in _TRIGGER_RE.finditer(text):
        if m.start() > last_end:
            seg = text[last_end:m.start()].rstrip(_PUNCT_BEFORE)
            if seg:
                ops.append(("text", seg))
        kind, val = _TRIGGERS[m.group(0).lower()]
        if kind == "key":
            ops.append(("key", val))
        else:
            ops.append(("char", val))
        last_end = m.end()
    if last_end < len(text):
        seg = text[last_end:].lstrip(_PUNCT_AFTER) if ops else text[last_end:]
        if seg:
            ops.append(("text", seg))
    return ops


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
        # If supplied, op_parser(text) -> ops list takes precedence over the
        # built-in regex parser. Lets app.py inject the LLM-backed parser.
        self._op_parser = op_parser or _split_into_ops

    def type(self, text: str) -> None:
        if not text:
            return
        ops = self._op_parser(text)
        if not ops:
            return
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
            # Inter-op pause so paste-target apps fully receive the previous
            # operation's events before the next one fires (especially
            # important between paste and key press).
            if i < len(ops) - 1:
                time.sleep(0.04)

    def _press_key(self, keycode: int) -> None:
        try:
            subprocess.run(
                ["ydotool", "key", f"{keycode}:1", f"{keycode}:0"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.CalledProcessError as e:
            raise InjectError(
                f"ydotool key {keycode} failed: {(e.stderr or '').strip() or e}"
            ) from e

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
        try:
            subprocess.run(
                [
                    "ydotool",
                    "key",
                    f"{_KEY_LCTRL}:1",
                    f"{_KEY_V}:1",
                    f"{_KEY_V}:0",
                    f"{_KEY_LCTRL}:0",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.CalledProcessError as e:
            raise InjectError(
                f"ydotool Ctrl+V failed: {(e.stderr or '').strip() or e}"
            ) from e


def make_backend(paste_mode: str = "auto", op_parser=None) -> YdotoolBackend:
    return YdotoolBackend(paste_mode=paste_mode, op_parser=op_parser)
