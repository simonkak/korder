"""Vision actions — screenshot a window, ask the LLM what it sees,
narrate the answer.

First action: ``describe_window``. Voice command shape:

    "describe what's in Firefox"
    "co widzisz w Firefoxie"
    "what's on the screen"

Mechanic: when a target is named, fuzzy-activate that window via
KWin (same matcher focus_window uses), then capture the now-active
window via Spectacle. The capture-after-activate approach has a
focus-changing side effect — the named window pops to the front —
which is acceptable for "describe X" queries because the user
implicitly wants attention on X anyway. Without a target, captures
whatever is currently active.

Vision API: Gemma 4 E4B is multimodal — Ollama's ``/api/generate``
accepts ``images: [base64_png]`` alongside the text prompt. We pass
a brief description prompt, language-hinted via the system locale,
and emit the result through the existing TTS / OSD progress bus."""
from __future__ import annotations
import base64
import json
import logging
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from korder import config, kwin
from korder.actions.base import Action, register
from korder.ui.i18n import current_locale, t, tf
from korder.ui.progress import emit_progress, emit_progress_speak

log = logging.getLogger(__name__)

_OLLAMA_URL = "http://localhost:11434/api/generate"
_SPECTACLE_TIMEOUT_S = 5.0
_VISION_TIMEOUT_S = 60.0
# Settle delay between activate_window_by_name and the screenshot —
# KWin takes a frame or two to commit the focus change. Without this
# the capture races and grabs the previous active window.
_FOCUS_SETTLE_S = 0.25


def _capture_active_window() -> str | None:
    """Capture the currently active window to a temp PNG. Returns the
    path (caller cleans up) or None on any failure — spectacle missing,
    no active window, write error.

    Uses --background --nonotify so Spectacle doesn't flash a UI or
    pop a "screenshot saved" notification while Korder is mid-action.
    """
    fd, path = tempfile.mkstemp(prefix="korder_vision_", suffix=".png")
    os.close(fd)
    try:
        result = subprocess.run(
            [
                "spectacle",
                "--background",
                "--nonotify",
                "--activewindow",
                "--output", path,
            ],
            capture_output=True,
            timeout=_SPECTACLE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("describe_window: spectacle failed: %s", e)
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    if result.returncode != 0:
        log.warning(
            "describe_window: spectacle rc=%d stderr=%r",
            result.returncode, result.stderr[:200],
        )
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        log.warning("describe_window: spectacle wrote no data to %s", path)
        return None
    return path


def _vision_describe(image_path: str, target_hint: str = "") -> str:
    """Send the image to Ollama with a brief-description prompt. Returns
    the model's text response, or empty string on any failure.

    Language-hinted via the user's system locale — Polish locales get
    a Polish prompt + answer; everything else falls back to English."""
    try:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("ascii")
    except OSError as e:
        log.warning("describe_window: failed to read screenshot %s: %s", image_path, e)
        return ""

    cfg = config.load()
    model = cfg["inject"]["llm_model"].strip() or "gemma4:e4b"
    locale = current_locale()
    if locale == "pl":
        instruction = (
            "Krótko opisz, co widać w tym oknie. 1-2 zdania, "
            "po polsku. Skup się na zawartości — tytule strony, "
            "głównych elementach, tekście."
        )
        if target_hint:
            instruction = (
                f"Użytkownik pyta o okno {target_hint!r}. " + instruction
            )
    else:
        instruction = (
            "Briefly describe what is visible in this window. "
            "1-2 sentences. Focus on content — page title, main "
            "elements, text."
        )
        if target_hint:
            instruction = (
                f"The user is asking about the {target_hint!r} window. "
                + instruction
            )

    # Note on num_predict: Gemma E4B's vision tokens (the embedded
    # image patches) are accounted against the prediction budget on
    # Ollama's side. A num_predict cap meant for "1-2 sentences of
    # output" left no headroom after the image was processed and the
    # response came back empty with done_reason='length'. Don't cap
    # output length here — let the model's natural stop token end
    # generation. Latency stays bounded by the temperature and the
    # short instruction.
    payload = {
        "model": model,
        "prompt": instruction,
        "images": [img_b64],
        "stream": False,
        "options": {"temperature": 0.2},
        "keep_alive": 300.0,
    }
    try:
        req = urllib.request.Request(
            _OLLAMA_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_VISION_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError) as e:
        log.warning("describe_window: vision call failed: %s", e)
        return ""
    return (body.get("response") or "").strip()


def _do_describe_window(target: str) -> None:
    target = (target or "").strip()
    locale = current_locale()
    lang = "pl" if locale == "pl" else "en"

    # 1. Focus the named window (best-effort). Even if matching fails
    # KWin returns success-iff-script-ran, so we capture whatever
    # ends up active and let the vision model describe that.
    if target:
        emit_progress(tf("progress_describe_focusing", name=target))
        kwin.activate_window_by_name(target)
        time.sleep(_FOCUS_SETTLE_S)

    # 2. Screenshot active window.
    emit_progress(t("progress_describe_capturing"))
    img_path = _capture_active_window()
    if img_path is None:
        emit_progress(t("progress_describe_capture_failed"))
        return

    # 3. Vision call. The OSD shows "Thinking" via the existing state
    # machinery once we return; the progress line here narrates the
    # specific stage we're in.
    try:
        emit_progress(t("progress_describe_thinking"))
        description = _vision_describe(img_path, target_hint=target)
    finally:
        try:
            os.unlink(img_path)
        except OSError:
            pass

    if not description:
        emit_progress(t("progress_describe_failed"))
        return

    # 4. Emit result. emit_progress_speak fires both the OSD progress
    # line and the TTS path (when [tts] enabled).
    emit_progress_speak(description, lang=lang)


def _describe_window_op(args: dict) -> tuple:
    target = (args or {}).get("target", "")
    if not isinstance(target, str):
        target = ""
    return ("callable", lambda t=target.strip(): _do_describe_window(t))


register(Action(
    name="describe_window",
    description=(
        "Take a screenshot of a window and describe its contents "
        "aloud via a vision model. Empty target → captures the active "
        "window. Named target → activates that window first (via "
        "fuzzy-match against open windows), then captures. "
        "USE for 'describe X' / 'co widzisz w X' / 'what's on screen' / "
        "equivalents. SKIP for non-vision questions."
    ),
    triggers={
        "en": [
            "describe",
            "what do you see",
            "what's on the screen",
            "what is on the screen",
        ],
        "pl": [
            "opisz",
            "co widzisz",
            "co jest na ekranie",
        ],
    },
    op_factory=_describe_window_op,
    tools=["list_open_windows"],
    parameters={
        "target": {
            "type": "string",
            "description": (
                "Optional app name or window-title fragment. Pick a "
                "literal value from list_open_windows results. Empty "
                "captures whatever is currently active."
            ),
        },
    },
))
