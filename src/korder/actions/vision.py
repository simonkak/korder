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
import re
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
# Vision pathway needs additional GPU work over a text-only forward
# pass — first call after a non-vision turn is meaningfully slower
# than steady-state. Smoke-tested at ~10s warm; bumping to 120s leaves
# headroom for a cold vision pathway load on shared GPU under load.
_VISION_TIMEOUT_S = 120.0
# Settle delay between activate_window_by_name and the screenshot —
# KWin takes a frame or two to commit the focus change. Without this
# the capture races and grabs the previous active window.
_FOCUS_SETTLE_S = 0.25

# Polish + English trigger words to strip when extracting a target
# from the user's transcript. Mirrors the Spotify override's drop
# list — when the LLM dispatched describe_window with empty target
# but the transcript clearly names a window, we extract here.
_DESCRIBE_VERBS = frozenset({
    "describe", "show",
    "opisz", "pokaż", "pokaz",
})
_DESCRIBE_FILLERS = frozenset({
    "the", "a", "an",
    "okno", "okno.",  # Polish "window"
    "window", "of",
    "what", "do", "you", "see", "in",
    "co", "widzisz", "w", "na",
    "is", "on", "screen",
})
_DESCRIBE_DROP = _DESCRIBE_VERBS | _DESCRIBE_FILLERS


def _extract_target_from_transcript(transcript: str) -> str:
    """Strip describe-action trigger words and common fillers from
    the transcript; what's left is the user's named target. Polish
    inflections may produce non-canonical forms ('Firefoxa' instead
    of 'Firefox') — we rely on KWin's downstream fuzzy matcher to
    normalize. Returns the joined remaining tokens or empty string.
    """
    out: list[str] = []
    for raw in re.split(r"[\s.,!?;:]+", transcript):
        if not raw:
            continue
        if raw.lower() in _DESCRIBE_DROP:
            continue
        out.append(raw)
    return " ".join(out)


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
            "Opisz krótko (1-2 zdania, po polsku) co użytkownik "
            "widzi w głównym obszarze tego okna — może to być "
            "strona internetowa, film, dokument, rozmowa, "
            "terminal, kod, edytor, gra, obraz, mapa, panel "
            "ustawień, lub cokolwiek innego. Pomiń pasek adresu, "
            "karty, paski narzędzi, menu, ikony, paski tytułowe i "
            "inne elementy interfejsu. Opisz konkretną zawartość "
            "którą widzisz; jeśli nie potrafisz odczytać treści "
            "lub obraz jest niejasny, powiedz to wprost zamiast "
            "zgadywać."
        )
        if target_hint:
            instruction = (
                f"Użytkownik pyta o okno {target_hint!r}. " + instruction
            )
    else:
        instruction = (
            "Briefly (1-2 sentences) describe what the user sees "
            "in the main area of this window — could be a webpage, "
            "video, document, conversation, terminal, code, "
            "editor, game, image, map, settings panel, or "
            "anything else. Skip the URL bar, tabs, toolbars, "
            "menus, icons, title bar, and other UI chrome. "
            "Describe the actual content visible; if you can't "
            "read the content or the image is unclear, say so "
            "directly instead of guessing."
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
    log.info("describe_window: starting, target=%r locale=%s", target, locale)

    # 1. Focus the named window (best-effort). Even if matching fails
    # KWin returns success-iff-script-ran, so we capture whatever
    # ends up active and let the vision model describe that.
    if target:
        emit_progress(tf("progress_describe_focusing", name=target))
        kwin.activate_window_by_name(target)
        time.sleep(_FOCUS_SETTLE_S)

    # 2. Screenshot active window.
    emit_progress(t("progress_describe_capturing"))
    t0 = time.time()
    img_path = _capture_active_window()
    log.info(
        "describe_window: capture %s in %.1fs",
        "ok" if img_path else "FAILED",
        time.time() - t0,
    )
    if img_path is None:
        emit_progress(t("progress_describe_capture_failed"))
        return

    # 3. Vision call. The OSD shows "Thinking" via the existing state
    # machinery once we return; the progress line here narrates the
    # specific stage we're in.
    try:
        emit_progress(t("progress_describe_thinking"))
        t0 = time.time()
        description = _vision_describe(img_path, target_hint=target)
        log.info(
            "describe_window: vision %s in %.1fs (%d chars)",
            "ok" if description else "EMPTY",
            time.time() - t0,
            len(description),
        )
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
    log.info("describe_window: speaking description (%d chars, lang=%s)", len(description), lang)
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
