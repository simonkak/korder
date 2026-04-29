from __future__ import annotations
import shutil
import subprocess
import time


class InjectError(RuntimeError):
    pass


# Linux evdev keycodes used for the Ctrl+V paste shortcut.
_KEY_LCTRL = 29
_KEY_V = 47


class YdotoolBackend:
    """Synthesize keystrokes via ydotool (uinput). For non-ASCII text on Wayland,
    routes through the clipboard with wl-copy + Ctrl+V because ydotool's keycode
    synthesis cannot produce characters that aren't single keys in the active keymap."""

    PASTE_AUTO = "auto"
    PASTE_ALWAYS = "always"
    PASTE_NEVER = "never"

    def __init__(self, paste_mode: str = "auto"):
        if shutil.which("ydotool") is None:
            raise InjectError(
                "ydotool not found in PATH. Install it (e.g. `sudo pacman -S ydotool`) "
                "and run scripts/setup-uinput.sh once."
            )
        self.paste_mode = paste_mode
        self._has_wl_copy = shutil.which("wl-copy") is not None

    def type(self, text: str) -> None:
        if not text:
            return
        if self._should_paste(text):
            self._paste(text)
        else:
            self._direct_type(text)

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
                capture_output=True,
                timeout=10,
            )
        except subprocess.CalledProcessError as e:
            raise InjectError(f"wl-copy failed: {(e.stderr or '').strip() or e}") from e
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


def make_backend(paste_mode: str = "auto") -> YdotoolBackend:
    return YdotoolBackend(paste_mode=paste_mode)
