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
from korder.ui.progress import (
    emit_progress,
    emit_progress_speak,
    request_keep_session_open,
)

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


# Stable copy of the most recent capture, kept across runs for
# debugging. The temp file used by the live call is unlinked after
# the vision call returns; this snapshot survives so the user can
# inspect what was actually screenshot when describe_window's output
# looks wrong.
_LAST_CAPTURE_DEBUG_PATH = "/tmp/korder_last_capture.png"


def _resolve_window_uuid(target: str) -> tuple[str, str]:
    """Look up the KWin internalId UUID for a window matching ``target``
    via the existing kwin_bridge window list. Token-overlap match
    (mirrors KWin's substring matcher). Returns (uuid, friendly_name)
    on success, ('', '') on failure (target not found, bridge offline)."""
    try:
        from korder import kwin_bridge
        windows = kwin_bridge.list_windows(timeout_s=1.0) or []
    except Exception:
        return ("", "")
    if not windows:
        return ("", "")
    target_tokens = {t.lower() for t in re.findall(r"\w+", target)}
    if not target_tokens:
        return ("", "")
    best: tuple[int, dict] | None = None
    for w in windows:
        if not isinstance(w, dict):
            continue
        klass = (w.get("resourceClass") or "").lower()
        caption = (w.get("caption") or "").lower()
        haystack_tokens = set(re.findall(r"\w+", klass + " " + caption))
        score = 0
        for t in target_tokens:
            if t in haystack_tokens:
                score += 2
            elif len(t) >= 3:
                for h in haystack_tokens:
                    if len(h) >= 3 and (t in h or h in t):
                        score += 1
                        break
        if score > 0 and (best is None or score > best[0]):
            best = (score, w)
    if best is None:
        return ("", "")
    win = best[1]
    uuid = (win.get("id") or "").strip()
    friendly = (win.get("resourceClass") or win.get("caption") or "").strip()
    return (uuid, friendly)


def _capture_window_by_uuid(uuid: str) -> str | None:
    """Capture a specific window by its KWin internalId UUID via the
    org.kde.KWin.ScreenShot2 D-Bus interface. No focus change — the
    window stays where it is. Returns the saved-PNG path or None on
    any failure (D-Bus not available, KWin doesn't recognize the UUID,
    write error, empty file).

    Plasma 6 only — ScreenShot2 was introduced in Plasma 5.27 and
    has been stable since 6.0. The interface returns metadata in a
    QVariantMap; we don't read it (filename is fixed by our FD)."""
    try:
        from PySide6.QtDBus import (
            QDBusConnection,
            QDBusInterface,
            QDBusUnixFileDescriptor,
        )
    except ImportError:
        return None
    fd, path = tempfile.mkstemp(prefix="korder_kwin_capture_", suffix=".png")
    try:
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            os.close(fd)
            os.unlink(path)
            return None
        iface = QDBusInterface(
            "org.kde.KWin",
            "/org/kde/KWin/ScreenShot2",
            "org.kde.KWin.ScreenShot2",
            bus,
        )
        if not iface.isValid():
            os.close(fd)
            os.unlink(path)
            return None
        # CaptureWindow(handle: str, options: dict, pipe: fd) → metadata
        reply = iface.call(
            "CaptureWindow",
            uuid,
            {},
            QDBusUnixFileDescriptor(fd),
        )
        # KWin has dup'd our FD into its own copy; close ours so the
        # tempfile inode isn't held open longer than needed.
        try:
            os.close(fd)
        except OSError:
            pass
        # call() returns a QDBusMessage; check for D-Bus-level error.
        err = reply.errorMessage() if hasattr(reply, "errorMessage") else ""
        if err:
            log.warning("ScreenShot2 CaptureWindow error: %s", err)
            try:
                os.unlink(path)
            except OSError:
                pass
            return None
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            log.warning("ScreenShot2 wrote no data to %s", path)
            try:
                os.unlink(path)
            except OSError:
                pass
            return None
        # Stable debug snapshot, same as the spectacle path.
        try:
            import shutil
            shutil.copyfile(path, _LAST_CAPTURE_DEBUG_PATH)
        except OSError as e:
            log.warning("describe_window: debug snapshot copy failed: %s", e)
        return path
    except Exception as e:
        log.warning("ScreenShot2 CaptureWindow raised: %s", e)
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass
        return None


def _capture_target_window(target: str) -> str | None:
    """Shared target-to-screenshot dispatcher used by every vision-class
    action (describe_window, read_screen_text, future ones).

    Empty target → captures the currently active window via
    Spectacle. Named target → tries ScreenShot2's CaptureWindow by
    UUID first (no focus change); falls back to activate-then-
    spectacle if UUID lookup fails or ScreenShot2 doesn't return a
    usable file. Logs which method actually ran so a wrong-window
    capture is easy to diagnose."""
    target = (target or "").strip()
    if not target:
        path = _capture_active_window()
        active = _peek_active_window_caption()
        log.info(
            "vision capture: method=spectacle-active, active=%r, path=%r",
            active, _LAST_CAPTURE_DEBUG_PATH if path else "(none)",
        )
        return path

    uuid, friendly = _resolve_window_uuid(target)
    if uuid:
        path = _capture_window_by_uuid(uuid)
        if path:
            log.info(
                "vision capture: method=screenshot2, target=%r→%r, "
                "uuid=%s…, path=%r",
                target, friendly, uuid[:8],
                _LAST_CAPTURE_DEBUG_PATH,
            )
            return path
        log.info(
            "vision capture: ScreenShot2 failed for uuid=%s — falling back",
            uuid[:8] + "…" if uuid else "(none)",
        )
    else:
        log.info(
            "vision capture: UUID lookup miss for target=%r — falling back",
            target,
        )

    # Fallback: activate + spectacle. Same path as before — has the
    # focus-changing side effect, but works when the bridge is
    # unavailable or KWin doesn't recognize the UUID.
    kwin.activate_window_by_name(target)
    time.sleep(_FOCUS_SETTLE_S)
    path = _capture_active_window()
    active = _peek_active_window_caption()
    log.info(
        "vision capture: method=spectacle-fallback, active=%r, path=%r",
        active, _LAST_CAPTURE_DEBUG_PATH if path else "(none)",
    )
    return path


def _ocr_image(image_path: str, langs: str = "pol+eng") -> str:
    """Run Tesseract on a screenshot, return the extracted plain
    text. Empty string on any failure (binary missing, lang pack
    missing, image unreadable). Polish + English by default — both
    are commonly installed on a Polish KDE install and Tesseract
    handles mixed-language pages reasonably."""
    try:
        result = subprocess.run(
            ["tesseract", image_path, "-", "-l", langs],
            capture_output=True,
            timeout=30,
            check=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("ocr: tesseract failed: %s", e)
        return ""
    if result.returncode != 0:
        log.warning(
            "ocr: tesseract rc=%d stderr=%r",
            result.returncode,
            (result.stderr or "")[:200],
        )
        return ""
    return (result.stdout or "").strip()


def _copy_to_clipboard(text: str) -> bool:
    """Push ``text`` to the Wayland clipboard via wl-copy. Returns
    True iff the byte stream made it past stdin into wl-copy's
    process; True is "best-effort committed", not "definitely on the
    clipboard."

    Implementation note: wl-copy daemonizes — it forks a child that
    stays alive holding the clipboard contents until something
    pastes (or the user clears it). subprocess.run() with timeout
    waits for ALL processes (including the daemon child) to exit,
    so it consistently times out at 5 s on a healthy wl-copy.

    Use Popen instead: write the bytes, close stdin, give wl-copy a
    short window to fork-and-detach, then return without waiting on
    the daemon. Mirrors how shells handle background pipelines."""
    if not text:
        return False
    try:
        proc = subprocess.Popen(
            ["wl-copy"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("clipboard: wl-copy spawn failed: %s", e)
        return False
    try:
        if proc.stdin is None:
            proc.kill()
            return False
        proc.stdin.write(text.encode("utf-8"))
        proc.stdin.close()
    except (OSError, BrokenPipeError) as e:
        log.warning("clipboard: wl-copy stdin write failed: %s", e)
        try:
            proc.kill()
        except Exception:
            pass
        return False
    # Brief wait for the parent to fork-and-exit. If wl-copy is
    # daemonizing properly the parent exits in milliseconds; the
    # backgrounded child keeps the clipboard alive on its own.
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        # Still running — that's fine, it's the daemon child holding
        # the clipboard. We're committed.
        pass
    return True


def _peek_active_window_caption() -> str:
    """Best-effort: ask the kwin_bridge which window is currently
    active. Returns 'class: caption' or empty string on any failure.
    Used purely for diagnostic logging during describe_window so we
    can tell whether the focus switch actually landed on the target.
    """
    try:
        from korder import kwin_bridge
        windows = kwin_bridge.list_windows(timeout_s=0.5) or []
    except Exception:
        return ""
    for w in windows:
        if isinstance(w, dict) and w.get("active"):
            klass = (w.get("resourceClass") or "").strip()
            caption = (w.get("caption") or "").strip()
            if klass and caption:
                return f"{klass}: {caption}"
            return klass or caption
    return ""


def _capture_active_window() -> str | None:
    """Capture the currently active window to a temp PNG. Returns the
    path (caller cleans up) or None on any failure — spectacle missing,
    no active window, write error.

    Uses --background --nonotify so Spectacle doesn't flash a UI or
    pop a "screenshot saved" notification while Korder is mid-action.

    Also writes a debug snapshot to _LAST_CAPTURE_DEBUG_PATH (stable
    path) so the user can inspect the captured frame after the live
    temp file is unlinked.
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
    # Debug snapshot — best-effort copy alongside the live temp file
    # so the user can inspect the last capture even after the live
    # path is unlinked.
    try:
        import shutil
        shutil.copyfile(path, _LAST_CAPTURE_DEBUG_PATH)
    except OSError as e:
        log.warning("describe_window: debug snapshot copy failed: %s", e)
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
    # Retry once on empty response. Field log: Gemma E4B's vision
    # pathway intermittently emits a stop token immediately after
    # the image patches with done_reason='stop' but response='' —
    # essentially a no-op generation. The same payload, retried,
    # produces a proper description. Costs ~10s extra on the rare
    # empty path; cheaper than failing the action.
    for attempt in range(2):
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
        result = (body.get("response") or "").strip()
        if result:
            return result
        log.info(
            "describe_window: vision returned empty (attempt %d/2, "
            "done_reason=%r, eval_count=%s)",
            attempt + 1, body.get("done_reason"), body.get("eval_count"),
        )
    return ""


def _do_describe_window(target: str) -> None:
    target = (target or "").strip()
    locale = current_locale()
    lang = "pl" if locale == "pl" else "en"
    log.info("describe_window: starting, target=%r locale=%s", target, locale)

    # 1+2. Capture target window (or active if no target).
    if target:
        emit_progress(tf("progress_describe_focusing", name=target))
    emit_progress(t("progress_describe_capturing"))
    img_path = _capture_target_window(target)
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

    # 4. Emit result. Three coordinated signals:
    #   - emit_progress_speak: renders to OSD + queues TTS.
    #   - request_keep_session_open: tells MainWindow to skip auto-
    #     stop so the user can ask a follow-up about what was just
    #     described, instead of needing a fresh wake-word activation.
    #   - post_synthetic_turn: writes a synthetic Turn into the
    #     parser's history so the next turn's prompt sees the
    #     description as if Gemma had emitted it as a `response` —
    #     follow-ups like 'and the title?' / 'a co jest na zdjęciu?'
    #     bind to the description without a re-dispatch.
    log.info(
        "describe_window: speaking description (%d chars, lang=%s)",
        len(description), lang,
    )
    request_keep_session_open()
    if locale == "pl":
        synthetic_user = (
            f"opisz okno {target}" if target else "opisz aktywne okno"
        )
    else:
        synthetic_user = (
            f"describe {target}" if target else "describe active window"
        )
    from korder.intent import post_synthetic_turn
    post_synthetic_turn(synthetic_user, description)
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
        "window. Named target → that window. "
        "Put the user's LITERAL app name into params.target — "
        "'Firefox' stays 'Firefox', NOT generalized to 'browser'; "
        "'Konsole' stays 'Konsole', NOT 'terminal'. Polish "
        "inflections like 'Firefoxa' are fine; the matcher handles "
        "them. "
        "USE for 'describe X' / 'co widzisz w X' / 'opisz okno X' / "
        "'what's on screen' / equivalents. SKIP for non-vision "
        "questions."
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
    # No tools=[]: the action's executor does its own window lookup
    # via _resolve_window_uuid (kwin_bridge → fuzzy-match), so the
    # LLM doesn't need to round-trip through list_open_windows
    # before dispatching. Field log: forcing list_open_windows on
    # iter 1 was nudging Gemma into a polite-decline pattern on
    # iter 2 ('I can describe the window — what would you like to
    # know?') instead of just dispatching. Letting the action
    # self-resolve is the simpler call.
    parameters={
        "target": {
            "type": "string",
            "description": (
                "Optional app name or window-title fragment. Empty "
                "captures whatever is currently active. Polish "
                "inflections like 'Firefoxa' work via the KWin "
                "matcher's substring fallback."
            ),
        },
    },
))


# ---- read_screen_text -------------------------------------------------


def _do_read_screen_text(target: str) -> None:
    target = (target or "").strip()
    locale = current_locale()
    lang = "pl" if locale == "pl" else "en"
    log.info("read_screen_text: starting, target=%r locale=%s", target, locale)

    emit_progress(t("progress_describe_capturing"))
    img_path = _capture_target_window(target)
    if img_path is None:
        emit_progress(t("progress_describe_capture_failed"))
        return

    try:
        emit_progress(t("progress_ocr_running"))
        t0 = time.time()
        text = _ocr_image(img_path)
        log.info(
            "read_screen_text: OCR %s in %.1fs (%d chars, %d words)",
            "ok" if text else "EMPTY",
            time.time() - t0,
            len(text),
            len(text.split()) if text else 0,
        )
    finally:
        try:
            os.unlink(img_path)
        except OSError:
            pass

    if not text:
        emit_progress(t("progress_ocr_empty"))
        return

    # Clipboard is a side effect; primary output is SPOKEN text.
    # 'przeczytaj' / 'read' = read aloud. The clipboard is a
    # convenience so the user can paste verbatim afterwards.
    _copy_to_clipboard(text)

    # Cap what we read aloud — long pages produce minutes of TTS
    # that no one actually wants. Cut at the nearest sentence
    # boundary inside the cap; append a short "rest is on clipboard"
    # note when truncated so the user knows the full text is
    # available without re-running the action.
    spoken_text = _trim_for_speech(text, locale=locale)
    log.info(
        "read_screen_text: speaking %d/%d chars (truncated=%s)",
        len(spoken_text),
        len(text),
        len(spoken_text) < len(text),
    )

    if locale == "pl":
        synthetic_user = (
            f"przeczytaj okno {target}" if target else "przeczytaj ekran"
        )
    else:
        synthetic_user = (
            f"read {target}" if target else "read the screen"
        )
    request_keep_session_open()
    from korder.intent import post_synthetic_turn
    # Synthetic response stores the FULL OCR text (capped) so
    # follow-ups can quote specifics — not just the spoken excerpt.
    # 1500-char cap keeps the prompt bounded.
    excerpt = text[:1500].strip()
    post_synthetic_turn(synthetic_user, excerpt)
    emit_progress_speak(spoken_text, lang=lang)


# Speech length cap. Past this, TTS playback gets long enough that
# "read me this" stops feeling responsive — the user might want to
# cancel and have the rest in clipboard. Sentence-aware so we don't
# cut mid-word.
_SPEECH_CAP_CHARS = 800


def _trim_for_speech(text: str, *, locale: str) -> str:
    """Cap ``text`` at ~_SPEECH_CAP_CHARS, breaking on a sentence
    boundary when possible. Appends a short "rest on clipboard"
    note (locale-aware) when truncation actually happens. The full
    text is in the clipboard regardless; this helper only shapes
    what the TTS engine reads aloud."""
    text = text.strip()
    if len(text) <= _SPEECH_CAP_CHARS:
        return text
    head = text[:_SPEECH_CAP_CHARS]
    # Prefer a strong sentence boundary near the cap so we don't
    # cut mid-clause. Fall back to the cap if nothing usable found.
    boundary = max(
        head.rfind(". "),
        head.rfind("? "),
        head.rfind("! "),
        head.rfind("\n"),
    )
    if boundary > _SPEECH_CAP_CHARS // 2:
        head = head[: boundary + 1]
    head = head.rstrip()
    if locale == "pl":
        suffix = " Reszta w schowku."
    else:
        suffix = " The rest is on the clipboard."
    return head + suffix


def _read_screen_text_op(args: dict) -> tuple:
    target = (args or {}).get("target", "")
    if not isinstance(target, str):
        target = ""
    return ("callable", lambda t=target.strip(): _do_read_screen_text(t))


register(Action(
    name="read_screen_text",
    description=(
        "Read the visible text of a window aloud via TTS (OCR'd by "
        "Tesseract, then spoken). Long content is capped at a few "
        "sentences for speech with the rest pushed to the clipboard "
        "as a side effect. Empty target → reads the active window. "
        "Named target → reads that window. "
        "USE for 'przeczytaj ekran' / 'przeczytaj okno X' / 'read "
        "the screen' / 'read me what's on this page' — the user "
        "wants to HEAR the content. "
        "SKIP for clipboard-only intents (no spoken output requested) "
        "and for vision-style queries about content meaning — that's "
        "describe_window."
    ),
    triggers={
        "en": [
            "read the screen",
            "read screen",
            "extract text",
            "ocr",
        ],
        "pl": [
            "przeczytaj ekran",
            "skopiuj tekst",
            "wyciągnij tekst",
            "wyciagnij tekst",
        ],
    },
    op_factory=_read_screen_text_op,
    # No tools=[] — same rationale as describe_window. The action
    # self-resolves via _resolve_window_uuid; forcing the tool was
    # nudging the LLM into a clarification-asking pattern instead of
    # straight dispatch.
    parameters={
        "target": {
            "type": "string",
            "description": (
                "Optional app name or window-title fragment. Empty "
                "reads whatever is currently active."
            ),
        },
    },
))
