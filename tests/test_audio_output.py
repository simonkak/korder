"""Tests for audio_output_switch — mocks `wpctl status` / `wpctl
set-default` so the tests are independent of the host's PipeWire."""
from __future__ import annotations
from unittest.mock import patch

import korder.actions  # noqa: F401  (default registrations)
from korder.actions import audio_output
from korder.actions.base import get_action


class _Run:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


# Sample output captured from the real wpctl on a desktop with both
# the monitor speakers (default) and a connected Denon soundbar. The
# Filters block contains spotify_mic_sink — must NOT bleed into the
# sink list.
_WPCTL_STATUS_TWO_SINKS = """\
PipeWire 'pipewire-0' [1.6.4]
 └─ Clients:
        33. WirePlumber

Audio
 ├─ Devices:
 │      63. Navi 31 HDMI/DP Audio               [alsa]
 │      67. USB Audio                           [alsa]
 │
 ├─ Sinks:
 │  *   59. Głośniki monitora                 [vol: 0.45]
 │      95. Denon DHT-S517                      [vol: 0.70]
 │
 ├─ Sources:
 │  *   74. C505 HD Webcam Mono                 [vol: 1.00]
 │
 ├─ Filters:
 │      37. spotify_mic_sink                   [Audio/Sink]
 │
 └─ Streams:
        92. ALSA plug-in
"""

_WPCTL_STATUS_NO_DEFAULT = """\
Audio
 ├─ Sinks:
 │      59. Głośniki monitora                 [vol: 0.45]
 │
 └─ Streams:
"""


# ---- _parse_wpctl_status -------------------------------------------------


def test_parse_status_extracts_sinks_and_default():
    sinks, default_id = audio_output._parse_wpctl_status(_WPCTL_STATUS_TWO_SINKS)
    assert sinks == [
        (59, "Głośniki monitora"),
        (95, "Denon DHT-S517"),
    ]
    assert default_id == 59


def test_parse_status_excludes_filter_sinks():
    """spotify_mic_sink lives under Filters, not Sinks. The parser
    stops at the next box-drawing branch character so it must NOT
    surface there."""
    sinks, _ = audio_output._parse_wpctl_status(_WPCTL_STATUS_TWO_SINKS)
    names = [n for _, n in sinks]
    assert "spotify_mic_sink" not in names


def test_parse_status_handles_missing_default_marker():
    """No '*' on any line — default_id is None, sinks still populated."""
    sinks, default_id = audio_output._parse_wpctl_status(_WPCTL_STATUS_NO_DEFAULT)
    assert sinks == [(59, "Głośniki monitora")]
    assert default_id is None


def test_parse_status_handles_empty_output():
    sinks, default_id = audio_output._parse_wpctl_status("")
    assert sinks == []
    assert default_id is None


# ---- _resolve_sink -------------------------------------------------------


def test_resolve_sink_picks_brand_match():
    sinks = [
        (59, "Głośniki monitora"),
        (95, "Denon DHT-S517"),
    ]
    assert audio_output._resolve_sink("Denon", sinks) == (95, "Denon DHT-S517")
    assert audio_output._resolve_sink("monitora", sinks) == (59, "Głośniki monitora")


def test_resolve_sink_returns_none_on_no_match():
    sinks = [(59, "Głośniki monitora")]
    assert audio_output._resolve_sink("HDMI", sinks) is None
    assert audio_output._resolve_sink("", sinks) is None
    assert audio_output._resolve_sink("anything", []) is None


def test_resolve_sink_handles_truncated_form():
    """Query token is a truncated form of a sink token (or vice
    versa). Substring containment rescues the match where exact
    equality misses. Generic case — covers morphological variation
    in any language, plus user shorthand ('head' for 'headphones')."""
    sinks = [(7, "Wireless Headphones")]
    assert audio_output._resolve_sink("headphone", sinks) == (
        7, "Wireless Headphones",
    )


def test_resolve_sink_handles_slug_token_split():
    """An underscore-separated input like 'monitor_speakers' must
    decompose into separate tokens during matching. The token regex
    excludes underscore so the slug tokenizes as {monitor, speakers}
    rather than staying a single opaque blob."""
    sinks = [
        (1, "Monitor Audio"),
        (2, "Some Other Sink"),
    ]
    assert audio_output._resolve_sink("monitor_speakers", sinks) == (
        1, "Monitor Audio",
    )


def test_resolve_sink_prefers_exact_over_fuzzy_only():
    """Two candidates: one matches exactly on a token, the other
    only via substring containment. Exact equality must outrank
    fuzzy-only so genuine direct hits aren't shadowed by lookalike
    candidates."""
    sinks = [
        (1, "Denon DHT-S517"),
        (2, "Denona Spare"),  # 'denona' contains 'denon'
    ]
    assert audio_output._resolve_sink("denon", sinks) == (1, "Denon DHT-S517")


# ---- registration --------------------------------------------------------


def test_audio_output_action_registered():
    action = get_action("audio_output_switch")
    assert action is not None
    assert "sink_name" in action.parameters
    assert action.parameters["sink_name"].get("required") is True


def test_audio_output_action_declares_discovery_tool():
    """The action references list_audio_sinks so the iterative loop
    in IntentParser can advertise it on turn 1."""
    action = get_action("audio_output_switch")
    assert "list_audio_sinks" in action.tools


def test_switch_op_with_empty_name_returns_pending():
    action = get_action("audio_output_switch")
    assert action.op_factory({}) is None
    assert action.op_factory({"sink_name": ""}) is None
    assert action.op_factory({"sink_name": "  "}) is None


# ---- end-to-end dispatch -------------------------------------------------


def test_switch_dispatches_set_default():
    """User says 'Denon' → resolver picks id=95 → wpctl set-default 95."""
    captured: list[list[str]] = []

    def fake_run(cmd, **kw):
        captured.append(list(cmd))
        if cmd[:2] == ["wpctl", "status"]:
            return _Run(_WPCTL_STATUS_TWO_SINKS)
        if cmd[:2] == ["wpctl", "set-default"]:
            return _Run("", returncode=0)
        return _Run("", returncode=1)

    with (
        patch("korder.actions.audio_output.shutil.which", return_value="/usr/bin/wpctl"),
        patch("korder.actions.audio_output.subprocess.run", side_effect=fake_run),
    ):
        action = get_action("audio_output_switch")
        kind, fn = action.op_factory({"sink_name": "Denon"})
        assert kind == "callable"
        fn()

    set_default_calls = [c for c in captured if c[:2] == ["wpctl", "set-default"]]
    assert set_default_calls == [["wpctl", "set-default", "95"]], (
        f"expected single set-default 95 call; got {captured!r}"
    )


def test_switch_skips_when_already_default():
    """Sink that's already the default → narrate 'already on this'
    and skip set-default."""
    captured: list[list[str]] = []

    def fake_run(cmd, **kw):
        captured.append(list(cmd))
        if cmd[:2] == ["wpctl", "status"]:
            return _Run(_WPCTL_STATUS_TWO_SINKS)
        return _Run("", returncode=0)

    with (
        patch("korder.actions.audio_output.shutil.which", return_value="/usr/bin/wpctl"),
        patch("korder.actions.audio_output.subprocess.run", side_effect=fake_run),
    ):
        action = get_action("audio_output_switch")
        # 'monitora' resolves to the current default (id=59).
        _, fn = action.op_factory({"sink_name": "monitora"})
        fn()

    assert all(c[:2] != ["wpctl", "set-default"] for c in captured), (
        f"set-default should be skipped when already on the sink; got {captured!r}"
    )


def test_switch_narrates_not_found_when_no_match():
    """Resolver miss → no set-default call. We don't blow up; the
    progress bus narrates 'sink not found: HDMI'."""
    captured: list[list[str]] = []

    def fake_run(cmd, **kw):
        captured.append(list(cmd))
        if cmd[:2] == ["wpctl", "status"]:
            return _Run(_WPCTL_STATUS_TWO_SINKS)
        return _Run("", returncode=0)

    with (
        patch("korder.actions.audio_output.shutil.which", return_value="/usr/bin/wpctl"),
        patch("korder.actions.audio_output.subprocess.run", side_effect=fake_run),
    ):
        action = get_action("audio_output_switch")
        _, fn = action.op_factory({"sink_name": "HDMI"})
        fn()

    assert all(c[:2] != ["wpctl", "set-default"] for c in captured)
