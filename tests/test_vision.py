"""Tests for the describe_window action.

The action's executor chains three subprocess/network calls (kwin
activate, spectacle capture, ollama vision). Each is mocked
individually so the tests are independent of the host's KDE / Spectacle
/ Ollama state."""
from __future__ import annotations
import json
import os
from unittest.mock import patch

import korder.actions  # noqa: F401  (register vision)
from korder.actions import vision
from korder.actions.base import get_action


# ---- registration --------------------------------------------------------


def test_describe_window_action_registered():
    action = get_action("describe_window")
    assert action is not None
    assert "target" in action.parameters


def test_describe_window_declares_discovery_tool():
    """list_open_windows is the canonical-name source for `target`."""
    action = get_action("describe_window")
    assert "list_open_windows" in action.tools


def test_describe_window_op_with_no_target_returns_callable():
    """Empty target falls through to capturing the active window —
    not a pending param."""
    action = get_action("describe_window")
    op = action.op_factory({})
    assert op is not None
    assert op[0] == "callable"


# ---- _extract_target_from_transcript -----------------------------------


def test_extract_target_from_polish_imperative():
    """'Opisz okno Firefoxa' — strip 'opisz', 'okno', leaves 'Firefoxa'.
    Polish inflection is preserved as-is; KWin's fuzzy match downstream
    handles morphology."""
    assert vision._extract_target_from_transcript("Opisz okno Firefoxa") == "Firefoxa"


def test_extract_target_from_english_imperative():
    assert vision._extract_target_from_transcript("describe Firefox") == "Firefox"


def test_extract_target_from_what_do_you_see_form():
    """'What do you see in Firefox' / 'co widzisz w Firefoxie'."""
    assert vision._extract_target_from_transcript("what do you see in Firefox") == "Firefox"
    assert vision._extract_target_from_transcript("co widzisz w Firefoxie") == "Firefoxie"


def test_extract_target_drops_punctuation():
    """Trailing '?' or '.' from Whisper shouldn't end up in the target."""
    assert vision._extract_target_from_transcript("Opisz okno Firefoxa?") == "Firefoxa"
    assert vision._extract_target_from_transcript("describe Firefox.") == "Firefox"


def test_extract_target_returns_empty_when_no_target():
    """Bare describe verb with no named target — nothing to extract."""
    assert vision._extract_target_from_transcript("Opisz") == ""
    assert vision._extract_target_from_transcript("co widzisz") == ""


# ---- _maybe_describe_window_target_fill (parse-layer override) ---------


def test_target_fill_when_llm_emitted_empty_target():
    """Field log: 'Opisz okno Firefoxa' → LLM emitted describe_window
    with empty target. Override extracts 'Firefoxa' from transcript."""
    from korder.intent import _maybe_describe_window_target_fill
    actions = [{"phrase": "describe", "name": "describe_window"}]
    override = _maybe_describe_window_target_fill(
        "Opisz okno Firefoxa", actions,
    )
    assert override is not None
    assert override["name"] == "describe_window"
    assert override["params"]["target"] == "Firefoxa"


def test_target_fill_skipped_when_llm_already_filled():
    """LLM did its job — don't second-guess."""
    from korder.intent import _maybe_describe_window_target_fill
    actions = [{
        "phrase": "describe",
        "name": "describe_window",
        "params": {"target": "Firefox"},
    }]
    override = _maybe_describe_window_target_fill(
        "Opisz okno Firefoxa", actions,
    )
    assert override is None


def test_target_fill_skipped_when_no_target_in_transcript():
    """Bare verb — nothing to extract, leave the empty-target action
    alone (it'll capture the active window, which is a sensible
    default for 'describe what you see')."""
    from korder.intent import _maybe_describe_window_target_fill
    actions = [{"phrase": "Opisz", "name": "describe_window"}]
    override = _maybe_describe_window_target_fill("Opisz", actions)
    assert override is None


def test_target_fill_skipped_when_action_is_not_describe_window():
    """Other actions don't get this override — only describe_window."""
    from korder.intent import _maybe_describe_window_target_fill
    actions = [{"phrase": "press enter", "name": "press_enter"}]
    override = _maybe_describe_window_target_fill(
        "press enter", actions,
    )
    assert override is None


# ---- executor flow -------------------------------------------------------


def _patch_capture(retval: str | None):
    return patch("korder.actions.vision._capture_active_window", return_value=retval)


def _patch_vision(retval: str):
    return patch("korder.actions.vision._vision_describe", return_value=retval)


def test_describe_window_full_flow_emits_speakable():
    """Happy path: target named → activate → capture → vision call →
    emit_progress_speak with the model's description."""
    spoken: list[tuple[str, str]] = []

    def fake_speak(text, lang="auto"):
        spoken.append((text, lang))

    with (
        patch("korder.kwin.activate_window_by_name", return_value=True) as activate,
        _patch_capture("/tmp/fake.png"),
        _patch_vision("Firefox is showing GitHub's pull request page."),
        patch("korder.actions.vision.emit_progress_speak", side_effect=fake_speak),
        patch("korder.actions.vision.os.unlink"),
        patch("korder.actions.vision.time.sleep"),
    ):
        action = get_action("describe_window")
        _, fn = action.op_factory({"target": "Firefox"})
        fn()

    activate.assert_called_once_with("Firefox")
    assert len(spoken) == 1
    text, lang = spoken[0]
    assert "Firefox" in text or "GitHub" in text
    assert lang in ("pl", "en")


def test_describe_window_no_target_skips_activate():
    """Empty target → don't fuzzy-activate, just capture the active
    window directly. Avoids changing focus when the user just said
    'what's on the screen?' style queries."""
    with (
        patch("korder.kwin.activate_window_by_name") as activate,
        _patch_capture("/tmp/fake.png"),
        _patch_vision("A terminal showing some output."),
        patch("korder.actions.vision.emit_progress_speak"),
        patch("korder.actions.vision.os.unlink"),
        patch("korder.actions.vision.time.sleep"),
    ):
        action = get_action("describe_window")
        _, fn = action.op_factory({})
        fn()
    activate.assert_not_called()


def test_describe_window_capture_failure_emits_progress_only():
    """Spectacle failed — progress lines fire, vision call does NOT,
    and no speakable output. The exact progress text is locale-
    dependent (PL/EN), so we assert behavioral invariants rather than
    string content."""
    spoken: list[tuple[str, str]] = []
    progress: list[str] = []

    with (
        patch("korder.kwin.activate_window_by_name", return_value=True),
        _patch_capture(None),
        _patch_vision("ignored") as vision_call,
        patch("korder.actions.vision.emit_progress",
              side_effect=lambda t: progress.append(t)),
        patch("korder.actions.vision.emit_progress_speak",
              side_effect=lambda t, lang="auto": spoken.append((t, lang))),
        patch("korder.actions.vision.time.sleep"),
    ):
        action = get_action("describe_window")
        _, fn = action.op_factory({"target": "Firefox"})
        fn()
    vision_call.assert_not_called()
    assert spoken == []
    # Two progress lines: focusing + capture-failed. No vision step.
    assert len(progress) >= 2


def test_describe_window_vision_failure_emits_progress_only():
    """Vision call returned empty → progress line, no speakable."""
    spoken: list[tuple[str, str]] = []

    with (
        patch("korder.kwin.activate_window_by_name", return_value=True),
        _patch_capture("/tmp/fake.png"),
        _patch_vision(""),
        patch("korder.actions.vision.emit_progress_speak",
              side_effect=lambda t, lang="auto": spoken.append((t, lang))),
        patch("korder.actions.vision.os.unlink"),
        patch("korder.actions.vision.time.sleep"),
    ):
        action = get_action("describe_window")
        _, fn = action.op_factory({"target": "Firefox"})
        fn()
    assert spoken == []


def test_describe_window_cleans_up_temp_file_on_success():
    """The capture writes a temp PNG. After the vision call, it must
    be unlinked — leaving them around would clutter /tmp over time."""
    unlinked: list[str] = []

    with (
        patch("korder.kwin.activate_window_by_name", return_value=True),
        _patch_capture("/tmp/korder_vision_abc.png"),
        _patch_vision("desc"),
        patch("korder.actions.vision.emit_progress_speak"),
        patch("korder.actions.vision.os.unlink",
              side_effect=lambda p: unlinked.append(p)),
        patch("korder.actions.vision.time.sleep"),
    ):
        action = get_action("describe_window")
        _, fn = action.op_factory({"target": "Firefox"})
        fn()
    assert unlinked == ["/tmp/korder_vision_abc.png"]


def test_describe_window_cleans_up_temp_file_on_vision_error():
    """Even if the vision call raises, the temp file gets cleaned —
    via the try/finally in _do_describe_window."""
    unlinked: list[str] = []

    def vision_raise(*a, **kw):
        raise RuntimeError("network down")

    with (
        patch("korder.kwin.activate_window_by_name", return_value=True),
        _patch_capture("/tmp/korder_vision_xyz.png"),
        patch("korder.actions.vision._vision_describe",
              side_effect=vision_raise),
        patch("korder.actions.vision.emit_progress_speak"),
        patch("korder.actions.vision.os.unlink",
              side_effect=lambda p: unlinked.append(p)),
        patch("korder.actions.vision.time.sleep"),
    ):
        action = get_action("describe_window")
        _, fn = action.op_factory({"target": "Firefox"})
        try:
            fn()
        except RuntimeError:
            pass  # the test just verifies the unlink ran
    assert unlinked == ["/tmp/korder_vision_xyz.png"]


# ---- _capture_active_window --------------------------------------------


def test_capture_returns_path_on_spectacle_success(tmp_path):
    """Smoke check that the spectacle invocation shape passes through
    the expected flags. The actual subprocess is mocked."""
    captured_cmds: list[list] = []

    class _FakeRun:
        returncode = 0
        stderr = b""

    def fake_run(cmd, **kw):
        captured_cmds.append(list(cmd))
        # Simulate spectacle writing a tiny file at the output path.
        idx = cmd.index("--output")
        with open(cmd[idx + 1], "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")  # png magic, enough bytes
        return _FakeRun()

    with patch("korder.actions.vision.subprocess.run", side_effect=fake_run):
        path = vision._capture_active_window()
    try:
        assert path is not None
        assert os.path.exists(path)
        cmd = captured_cmds[0]
        assert "spectacle" in cmd[0]
        assert "--background" in cmd
        assert "--nonotify" in cmd
        assert "--activewindow" in cmd
        assert "--output" in cmd
    finally:
        if path and os.path.exists(path):
            os.unlink(path)


def test_capture_returns_none_on_spectacle_failure():
    class _FakeRun:
        returncode = 1
        stderr = b"spectacle: failed"

    with patch("korder.actions.vision.subprocess.run", return_value=_FakeRun()):
        assert vision._capture_active_window() is None


# ---- _vision_describe --------------------------------------------------


def test_vision_describe_sends_image_and_prompt():
    """The vision call should base64-encode the image and pass a
    descriptive prompt + the configured llm_model."""
    captured_payload: dict = {}

    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({"response": "A coding window."}).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        captured_payload.update(json.loads(req.data.decode("utf-8")))
        return _FakeResp()

    # Write a tiny test image
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".png")
    try:
        os.write(fd, b"\x89PNG\r\n\x1a\n" + b"x" * 100)
        os.close(fd)
        with (
            patch("korder.actions.vision.urllib.request.urlopen", side_effect=fake_urlopen),
            patch("korder.actions.vision.config.load",
                  return_value={"inject": {"llm_model": "gemma4:e4b"}}),
        ):
            result = vision._vision_describe(path, target_hint="Firefox")
        assert result == "A coding window."
        assert captured_payload["model"] == "gemma4:e4b"
        assert "images" in captured_payload
        assert len(captured_payload["images"]) == 1
        # base64 of png magic should start with iVBORw...
        assert captured_payload["images"][0].startswith("iVBOR")
        # Target hint should appear in the prompt.
        assert "Firefox" in captured_payload["prompt"]
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_vision_describe_returns_empty_on_network_error():
    import urllib.error
    with (
        patch("korder.actions.vision.urllib.request.urlopen",
              side_effect=urllib.error.URLError("conn refused")),
        patch("korder.actions.vision.config.load",
              return_value={"inject": {"llm_model": "gemma4:e4b"}}),
    ):
        # Use a path that exists so _vision_describe gets past the
        # file-read step and into the urlopen path.
        import tempfile
        fd, path = tempfile.mkstemp()
        try:
            os.write(fd, b"\x89PNG")
            os.close(fd)
            assert vision._vision_describe(path) == ""
        finally:
            if os.path.exists(path):
                os.unlink(path)
