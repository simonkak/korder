"""Vision-grounding fallback for click_by_label.

When AT-SPI returns no match (Electron without --force-renderer-
accessibility, Chromium subtrees, canvas UIs), capture the screen,
ask Gemma to localize the named control, parse the bounding box, and
return click coordinates.

Per Google's vision docs, Gemma 4 emits ``box_2d: [y1, x1, y2, x2]``
on a 1024×1024 normalized grid. We rescale to actual pixel coords
client-side.

Spectacle (KDE's screenshot tool, ships with Plasma) is used for the
capture. Pillow handles the resize + encode. ollama's /api/generate
accepts an ``images`` array of base64 strings.
"""
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

log = logging.getLogger(__name__)

_OLLAMA_URL = "http://localhost:11434/api/generate"

# Resize the screenshot to fit inside this box before sending. Caps
# input tokens; Gemma's 1024-grid output is independent of input size,
# so we lose nothing on the localization side. 1280×720 ≈ 5×3 tiles at
# Gemma's 256-px tile granularity ≈ ~1000 image tokens. Good detail
# vs. latency tradeoff for typical desktops.
_MAX_DIMENSION = 1280

# Per-call timeout. Vision inference on a 7800 XT typically lands
# between 1.5–4s; 15s gives plenty of headroom for cold loads.
_TIMEOUT_S = 15.0


def _capture_full_screen() -> str | None:
    """Spectacle-capture the entire desktop to a temp PNG. Returns the
    file path on success, None on capture failure. Full-screen scope
    keeps the bbox math simple — the resulting pixel coordinates are
    already absolute."""
    fd, path = tempfile.mkstemp(prefix="korder-click-", suffix=".png")
    os.close(fd)
    try:
        # -b background, -n no-notify, -f fullscreen, -o output, -e/-S strip
        # decorations/shadows (no-ops in fullscreen but cheap to keep)
        result = subprocess.run(
            ["spectacle", "-bnf", "-o", path],
            check=False, capture_output=True, timeout=5.0,
        )
        if result.returncode != 0:
            log.error(
                "spectacle exited %d: %s",
                result.returncode,
                (result.stderr or b"").decode(errors="replace").strip(),
            )
            try:
                os.unlink(path)
            except OSError:
                pass
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.error("spectacle capture failed: %s", e)
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        log.error("spectacle produced empty file at %s", path)
        return None
    return path


def _resize_and_encode(path: str) -> tuple[str, int, int] | None:
    """Resize PNG to fit inside _MAX_DIMENSION × _MAX_DIMENSION,
    base64-encode. Returns (b64_str, original_w, original_h) or None
    on failure. The original dimensions are needed to map the model's
    1024-grid output back to screen pixels."""
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        log.error("Pillow missing — install with `uv sync --extra a11y`")
        return None
    try:
        with Image.open(path) as im:
            orig_w, orig_h = im.size
            if max(orig_w, orig_h) > _MAX_DIMENSION:
                im.thumbnail((_MAX_DIMENSION, _MAX_DIMENSION), Image.Resampling.LANCZOS)
            buf_path = path + ".small.png"
            im.save(buf_path, format="PNG", optimize=True)
        with open(buf_path, "rb") as f:
            data = f.read()
        try:
            os.unlink(buf_path)
        except OSError:
            pass
        return base64.b64encode(data).decode("ascii"), orig_w, orig_h
    except Exception as e:
        log.error("PIL resize/encode failed: %s", e)
        return None


def _build_prompt(label: str) -> str:
    """Single-purpose vision prompt. We don't reuse the intent parser's
    prompt — that one is text-only and would confuse the model with an
    image attachment. Constrained JSON output makes parsing trivial."""
    return (
        f'Look at the screenshot. Find the on-screen control (button, link, '
        f'menu item, tab, or icon) labeled or showing the text "{label}". '
        f'Reply with JSON only: {{"box_2d": [y1, x1, y2, x2]}} on a 1024 by '
        f'1024 normalized grid, where y1<y2 and x1<x2. If no such control '
        f'is visible, reply {{"box_2d": null}}. Do not include any other '
        f'fields, prose, or markdown.'
    )


def _call_ollama_vision(model: str, prompt: str, image_b64: str) -> dict | None:
    """POST to /api/generate with format=json and the image. Returns the
    parsed response object on success, None on transport or parse
    failure."""
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.0},
    }
    try:
        req = urllib.request.Request(
            _OLLAMA_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        log.info("vision call: %s ms", f"{elapsed_ms:.0f}")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log.error("vision call failed: %s", e)
        return None
    response_str = body.get("response") or ""
    if not response_str:
        log.warning("vision call returned empty response")
        return None
    try:
        return json.loads(response_str)
    except json.JSONDecodeError as e:
        log.warning("vision response not JSON: %s | raw=%r", e, response_str[:200])
        return None


def _parse_box(parsed: dict, orig_w: int, orig_h: int) -> tuple[int, int] | None:
    """Extract box_2d, validate, scale to absolute pixel coords, return
    center. Returns None when the model said `null` or the payload is
    malformed."""
    box = parsed.get("box_2d")
    if box is None:
        return None
    if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)):
        log.warning("vision: malformed box_2d: %r", box)
        return None
    y1, x1, y2, x2 = box
    if not (0 <= x1 < x2 <= 1024 and 0 <= y1 < y2 <= 1024):
        log.warning("vision: box_2d out of grid bounds: %r", box)
        return None
    cx_grid = (x1 + x2) / 2.0
    cy_grid = (y1 + y2) / 2.0
    cx = int(cx_grid / 1024.0 * orig_w)
    cy = int(cy_grid / 1024.0 * orig_h)
    return cx, cy


def find_click_target(label: str, model: str) -> tuple[int, int] | None:
    """Full vision-fallback flow: capture → resize → ollama → parse.
    Returns (cx, cy) absolute screen coords or None on any failure
    along the way. Caller is responsible for narration and the actual
    click."""
    shot_path = _capture_full_screen()
    if shot_path is None:
        return None
    try:
        encoded = _resize_and_encode(shot_path)
        if encoded is None:
            return None
        b64, orig_w, orig_h = encoded
        log.info("vision: screenshot %dx%d for label=%r", orig_w, orig_h, label)
        prompt = _build_prompt(label)
        parsed = _call_ollama_vision(model, prompt, b64)
        if parsed is None:
            return None
        if isinstance(parsed, dict):
            log.info("vision: model returned %r", parsed)
        return _parse_box(parsed, orig_w, orig_h)
    finally:
        try:
            os.unlink(shot_path)
        except OSError:
            pass


def click_at(cx: int, cy: int) -> bool:
    """Move + click via ydotool. Same wire as the a11y coord-click
    fallback; duplicated here so the vision path doesn't depend on
    _a11y being importable."""
    import shutil
    if shutil.which("ydotool") is None:
        log.error("ydotool missing, cannot click")
        return False
    try:
        subprocess.run(
            ["ydotool", "mousemove", "--absolute", "-x", str(cx), "-y", str(cy)],
            check=True, capture_output=True, timeout=2.0,
        )
        subprocess.run(
            ["ydotool", "click", "0xC0"],
            check=True, capture_output=True, timeout=2.0,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.error("vision click at (%d,%d) failed: %s", cx, cy, e)
        return False
    return True
