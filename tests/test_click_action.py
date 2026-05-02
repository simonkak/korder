"""Unit tests for click_by_label — fuzzy matcher, normalization, the
AT-SPI/vision-fallback orchestrator, and registry/prompt integration.

The Atspi GI module is never imported here; we patch
``korder.actions._a11y.enumerate_active_window_clickables`` to return
plain dicts, and ``_a11y.click_widget`` to flag whether it was called.
Same pattern for the vision fallback.
"""
from __future__ import annotations

import korder.actions  # noqa: F401  (ensure registrations)
from korder.actions import _a11y, _vision_click, click as click_module
from korder.actions.base import all_actions, get_action
from korder.actions.parser import split_into_ops


# ---------- normalization / fuzzy match ----------

def test_strip_label_drops_mnemonic_and_diacritics():
    assert click_module._strip_label("&Submit") == "submit"
    assert click_module._strip_label("Wyślij") == "wyslij"
    assert click_module._strip_label("OK") == "ok"
    assert click_module._strip_label("Send (S)") == "send"
    assert click_module._strip_label("Open File…") == "open file"
    assert click_module._strip_label("Open File...") == "open file"
    assert click_module._strip_label("_Save") == "save"


def test_strip_label_handles_empty_and_whitespace():
    assert click_module._strip_label("") == ""
    assert click_module._strip_label("   ") == ""


def test_find_match_exact_after_normalization():
    widgets = [
        {"name": "&Submit", "x": 0, "y": 0, "w": 80, "h": 30},
        {"name": "Cancel",  "x": 100, "y": 0, "w": 80, "h": 30},
    ]
    m = click_module._find_match("submit", widgets)
    assert m is not None and m["name"] == "&Submit"


def test_find_match_polish_diacritic_fold():
    widgets = [{"name": "Wyślij", "x": 0, "y": 0, "w": 80, "h": 30}]
    m = click_module._find_match("wyslij", widgets)
    assert m is not None and m["name"] == "Wyślij"
    # Reverse direction also works (widget without diacritics, spoken with)
    widgets2 = [{"name": "Wyslij", "x": 0, "y": 0, "w": 80, "h": 30}]
    m2 = click_module._find_match("wyślij", widgets2)
    assert m2 is not None


def test_find_match_returns_none_below_cutoff():
    widgets = [{"name": "Cancel", "x": 0, "y": 0, "w": 80, "h": 30}]
    # "qqq" is clearly far from "cancel" (no shared letters); difflib won't
    # close-match it. (We deliberately don't test "camcel" — Whisper
    # one-letter mistranscriptions ARE within the 0.6 cutoff and SHOULD
    # match, that's the desirable robustness property.)
    assert click_module._find_match("qqq", widgets) is None
    assert click_module._find_match("zzzzz", widgets) is None


def test_find_match_picks_innermost_on_ambiguity():
    """When two widgets match equally well by name, the smaller one
    (innermost dialog) wins."""
    widgets = [
        # Big "OK" — backdrop
        {"name": "OK", "x": 0, "y": 0, "w": 200, "h": 60},
        # Small "OK" — modal child
        {"name": "OK", "x": 50, "y": 50, "w": 60, "h": 24},
    ]
    m = click_module._find_match("ok", widgets)
    assert m is not None
    assert m["w"] * m["h"] == 60 * 24


def test_find_match_empty_widget_list():
    assert click_module._find_match("submit", []) is None


def test_find_match_empty_label():
    widgets = [{"name": "Submit", "x": 0, "y": 0, "w": 80, "h": 30}]
    assert click_module._find_match("", widgets) is None


# ---------- registry + LLM prompt enumeration ----------

def test_action_is_registered():
    assert get_action("click_by_label") is not None
    assert any(a.name == "click_by_label" for a in all_actions())


def test_action_appears_in_llm_prompt_with_param():
    # Reuse the intent prompt builder so we exercise the actual code path
    # the LLM sees, not a reimplementation.
    from korder.intent import _render_action_catalogue
    rendered = _render_action_catalogue(show_triggers=False)
    assert "click_by_label" in rendered
    # Param key is enumerated explicitly
    line = next(ln for ln in rendered.splitlines() if ln.startswith("- click_by_label"))
    assert "[params: label]" in line


# ---------- pending-parameter flow ----------

def test_bare_click_emits_pending_action():
    """User says just 'click' — op_factory({}) returns None, regex parser
    emits a pending_action so the next utterance fills the label. Same
    contract as web_search and spotify_play."""
    ops = split_into_ops("click")
    assert ("pending_action", "click_by_label") in ops


def test_click_with_label_via_regex_returns_callable():
    """User says 'click submit' — regex parser captures 'submit' as the
    label and routes to a callable op."""
    ops = split_into_ops("click submit")
    assert any(op[0] == "callable" for op in ops), f"got {ops!r}"


def test_polish_kliknij_with_label():
    # Note: "wyślij" is itself a trigger for press_enter, so we use a
    # label that isn't ambiguous with another action's triggers.
    ops = split_into_ops("kliknij ustawienia")
    assert any(op[0] == "callable" for op in ops), f"got {ops!r}"


# ---------- orchestrator: AT-SPI hit vs vision fallback ----------

class _RecordingFakeA11y:
    """Stub for the _a11y module's two public entry points used by the
    click orchestrator. Tracks which path was exercised so tests can
    assert on it."""
    def __init__(self, widgets, click_succeeds=True):
        self.widgets = widgets
        self.click_succeeds = click_succeeds
        self.clicked: dict | None = None

    def is_available(self):
        return True

    def enumerate_active_window_clickables(self):
        return list(self.widgets)

    def click_widget(self, w):
        self.clicked = w
        return self.click_succeeds


class _RecordingFakeVision:
    def __init__(self, coords=None, click_succeeds=True):
        self.coords = coords
        self.click_succeeds = click_succeeds
        self.find_calls: list[str] = []
        self.click_calls: list[tuple[int, int]] = []

    def find_click_target(self, label, model):
        self.find_calls.append(label)
        return self.coords

    def click_at(self, cx, cy):
        self.click_calls.append((cx, cy))
        return self.click_succeeds


def _patch_modules(monkeypatch, fake_a11y=None, fake_vision=None, vision_enabled=True):
    if fake_a11y is not None:
        monkeypatch.setattr(click_module, "_a11y", fake_a11y)
    if fake_vision is not None:
        monkeypatch.setattr(click_module, "_vision_click", fake_vision)
    monkeypatch.setattr(click_module, "_vision_fallback_enabled", lambda: vision_enabled)
    monkeypatch.setattr(click_module, "_llm_model", lambda: "gemma4:e4b")


def test_at_spi_hit_skips_vision(monkeypatch):
    fake_a11y = _RecordingFakeA11y(
        widgets=[{"name": "Submit", "x": 0, "y": 0, "w": 80, "h": 30}],
    )
    fake_vision = _RecordingFakeVision(coords=(123, 456))
    _patch_modules(monkeypatch, fake_a11y, fake_vision)
    click_module._do_click("submit")
    assert fake_a11y.clicked is not None and fake_a11y.clicked["name"] == "Submit"
    assert fake_vision.find_calls == [], "vision should NOT be called when AT-SPI matched"


def test_at_spi_miss_engages_vision(monkeypatch):
    fake_a11y = _RecordingFakeA11y(
        widgets=[{"name": "Cancel", "x": 0, "y": 0, "w": 80, "h": 30}],
    )
    fake_vision = _RecordingFakeVision(coords=(123, 456))
    _patch_modules(monkeypatch, fake_a11y, fake_vision)
    click_module._do_click("submit")
    assert fake_a11y.clicked is None
    assert fake_vision.find_calls == ["submit"]
    assert fake_vision.click_calls == [(123, 456)]


def test_at_spi_miss_with_vision_disabled_does_nothing(monkeypatch):
    fake_a11y = _RecordingFakeA11y(
        widgets=[{"name": "Cancel", "x": 0, "y": 0, "w": 80, "h": 30}],
    )
    fake_vision = _RecordingFakeVision(coords=(123, 456))
    _patch_modules(monkeypatch, fake_a11y, fake_vision, vision_enabled=False)
    click_module._do_click("submit")
    assert fake_a11y.clicked is None
    assert fake_vision.find_calls == [], "vision should NOT be called when fallback disabled"


def test_vision_returns_none_no_click(monkeypatch):
    fake_a11y = _RecordingFakeA11y(widgets=[])
    fake_vision = _RecordingFakeVision(coords=None)
    _patch_modules(monkeypatch, fake_a11y, fake_vision)
    click_module._do_click("nonexistent")
    assert fake_vision.find_calls == ["nonexistent"]
    assert fake_vision.click_calls == [], "no click should be issued when vision found nothing"


def test_a11y_unavailable_falls_through_to_vision(monkeypatch):
    """When the a11y extra isn't installed, is_available() returns False;
    we still try vision fallback (when enabled) so the user gets some
    chance of clicking."""
    class _UnavailableA11y:
        def is_available(self):
            return False
        def enumerate_active_window_clickables(self):
            return []
        def click_widget(self, w):
            return False
    fake_vision = _RecordingFakeVision(coords=(50, 60))
    _patch_modules(monkeypatch, _UnavailableA11y(), fake_vision)
    click_module._do_click("submit")
    assert fake_vision.find_calls == ["submit"]
    assert fake_vision.click_calls == [(50, 60)]


def test_empty_label_no_op(monkeypatch):
    fake_a11y = _RecordingFakeA11y(widgets=[])
    fake_vision = _RecordingFakeVision()
    _patch_modules(monkeypatch, fake_a11y, fake_vision)
    click_module._do_click("")
    click_module._do_click("   ")
    assert fake_vision.find_calls == []


# ---------- vision parse helpers ----------

def test_vision_parse_box_center_and_scale():
    parsed = {"box_2d": [256, 256, 512, 768]}  # y1, x1, y2, x2
    out = _vision_click._parse_box(parsed, orig_w=2048, orig_h=1024)
    assert out is not None
    cx, cy = out
    # Center on grid: x = (256+768)/2 = 512, y = (256+512)/2 = 384
    # Scale: cx = 512/1024*2048 = 1024; cy = 384/1024*1024 = 384
    assert cx == 1024
    assert cy == 384


def test_vision_parse_box_null_returns_none():
    assert _vision_click._parse_box({"box_2d": None}, 1920, 1080) is None
    assert _vision_click._parse_box({}, 1920, 1080) is None


def test_vision_parse_box_malformed_returns_none():
    assert _vision_click._parse_box({"box_2d": [1, 2, 3]}, 1920, 1080) is None
    assert _vision_click._parse_box({"box_2d": "nope"}, 1920, 1080) is None
    # Out of bounds
    assert _vision_click._parse_box({"box_2d": [-1, 0, 100, 100]}, 1920, 1080) is None
    # Inverted (y2 < y1)
    assert _vision_click._parse_box({"box_2d": [500, 0, 100, 100]}, 1920, 1080) is None
