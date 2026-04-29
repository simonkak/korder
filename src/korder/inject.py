from __future__ import annotations
import os
import shutil
import subprocess


class InjectError(RuntimeError):
    pass


class YdotoolBackend:
    """Synthesize keystrokes via ydotool (uinput). Works on both X11 and Wayland."""

    def __init__(self):
        if shutil.which("ydotool") is None:
            raise InjectError(
                "ydotool not found in PATH. Install it (e.g. `sudo pacman -S ydotool`) "
                "and run scripts/setup-uinput.sh once."
            )

    def type(self, text: str) -> None:
        if not text:
            return
        env = os.environ.copy()
        try:
            subprocess.run(
                ["ydotool", "type", "--", text],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            hint = ""
            if "uinput" in stderr.lower() or "permission" in stderr.lower():
                hint = " (run scripts/setup-uinput.sh and start ydotoold)"
            raise InjectError(f"ydotool failed: {stderr or e}{hint}") from e


def make_backend() -> YdotoolBackend:
    return YdotoolBackend()
