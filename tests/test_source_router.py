"""Tests for korder.audio.source_router pactl output parsing."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from korder.audio import source_router as sr


PACTL_LIST_CARDS_OUTPUT = """\
Card #44
\tName: bluez_card.00_1B_66_E8_90_10
\tDriver: bluez_card.c
\tOwner Module: n/a
\tProperties:
\t\tdevice.description = "PXC 550-II"
\tProfiles:
\t\toff: Off (sinks: 0, sources: 0, priority: 0, available: yes)
\t\ta2dp-sink-sbc: A2DP SBC (sinks: 1, sources: 0, priority: 133, available: yes)
\t\ta2dp-sink: A2DP aptX (sinks: 1, sources: 0, priority: 135, available: yes)
\t\ta2dp-sink-aptx_ll: A2DP aptX-LL (sinks: 1, sources: 0, priority: 131, available: yes)
\t\theadset-head-unit-cvsd: HSP/HFP CVSD (sinks: 1, sources: 1, priority: 7, available: yes)
\t\theadset-head-unit: HSP/HFP mSBC (sinks: 1, sources: 1, priority: 8, available: yes)
\tActive Profile: a2dp-sink-aptx_ll
\tPorts:
\t\tsome-port: ...

Card #1
\tName: alsa_card.pci-0000_03_00.6
\tProfiles:
\t\toutput:hdmi: HDMI (sinks: 1, sources: 0, priority: 5500, available: no)
\tActive Profile: off
"""


def _mocked_run(cmd, **_kwargs):
    """Stand-in for subprocess.run that pretends pactl list cards was called."""
    if cmd[:3] == ["pactl", "list", "cards"]:
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=PACTL_LIST_CARDS_OUTPUT, stderr=""
        )
    if cmd[:2] == ["pactl", "set-card-profile"]:
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
    raise AssertionError(f"unexpected pactl invocation: {cmd}")


@pytest.fixture
def mocked_pactl():
    with patch.object(subprocess, "run", side_effect=_mocked_run):
        yield


def test_bt_card_name_uses_underscore_separated_mac():
    assert sr.bt_card_name("00:1B:66:E8:90:10") == "bluez_card.00_1B_66_E8_90_10"


def test_bt_input_source_uses_predicted_pipewire_naming():
    # The source name PipeWire materializes when a stream opens after HFP profile switch.
    assert sr.bt_input_source_for("00:1B:66:E8:90:10") == "bluez_input.00_1B_66_E8_90_10.0"


def test_get_active_profile_finds_correct_card(mocked_pactl):
    assert sr.get_active_profile("bluez_card.00_1B_66_E8_90_10") == "a2dp-sink-aptx_ll"


def test_get_active_profile_returns_none_for_unknown_card(mocked_pactl):
    assert sr.get_active_profile("bluez_card.no_such_device") is None


def test_get_active_profile_isolated_per_card(mocked_pactl):
    # Make sure we don't return the wrong card's active profile by accident
    # — alsa_card has Active Profile: off, BT card has aptX-LL.
    assert sr.get_active_profile("alsa_card.pci-0000_03_00.6") == "off"


def test_card_has_profile_positive(mocked_pactl):
    assert sr.card_has_profile("bluez_card.00_1B_66_E8_90_10", "headset-head-unit") is True


def test_card_has_profile_negative(mocked_pactl):
    assert sr.card_has_profile("bluez_card.00_1B_66_E8_90_10", "headset-head-unit-msbc") is False


def test_card_has_profile_doesnt_match_substring(mocked_pactl):
    # 'a2dp-sink' shouldn't false-positive due to 'a2dp-sink-sbc' etc.
    # (the matcher anchors on profile name + ':')
    assert sr.card_has_profile("bluez_card.00_1B_66_E8_90_10", "a2dp-sink") is True
    assert sr.card_has_profile("bluez_card.00_1B_66_E8_90_10", "a2dp") is False


def test_set_profile_returns_true_on_success(mocked_pactl):
    assert sr.set_profile("bluez_card.00_1B_66_E8_90_10", "headset-head-unit") is True
