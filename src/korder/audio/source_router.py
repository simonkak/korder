"""Bluetooth profile + source routing helpers built on `pactl`.

We don't use a Python PulseAudio binding (pulsectl) because:
- one less dep, and the surface we need is tiny;
- pactl works identically on PulseAudio and PipeWire+pipewire-pulse,
  so we don't need to detect which audio server is active.

PipeWire/WirePlumber subtlety: the BT input source for HFP is created
*lazily*. The card profile switches to `headset-head-unit`, but the
source object only appears when something opens it. That's why we
return a *predicted* source name from `bt_input_source_for(mac)` —
when sounddevice opens it, PipeWire materializes it.
"""
from __future__ import annotations

import re
import subprocess


def _pactl(*args: str, timeout_s: float = 3.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["pactl", *args],
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


def bt_card_name(mac: str) -> str:
    """Predict the PA card name for a given Bluetooth MAC."""
    return f"bluez_card.{mac.replace(':', '_')}"


def bt_input_source_for(mac: str) -> str:
    """Predict the PA source name for the HFP mic on a given MAC.

    Used as the `device` argument to sounddevice.InputStream — opening
    the stream materializes the source on PipeWire.
    """
    return f"bluez_input.{mac.replace(':', '_')}.0"


def get_active_profile(card: str) -> str | None:
    """Return the currently active profile name for `card`, or None."""
    res = _pactl("list", "cards")
    if res.returncode != 0:
        return None
    in_card = False
    for line in res.stdout.splitlines():
        if line.strip().startswith("Name: "):
            in_card = (line.strip() == f"Name: {card}")
            continue
        if in_card and "Active Profile:" in line:
            return line.split(":", 1)[1].strip()
    return None


def set_profile(card: str, profile: str) -> bool:
    """Switch a card to a profile. Returns True on success."""
    res = _pactl("set-card-profile", card, profile)
    return res.returncode == 0


def card_has_profile(card: str, profile: str) -> bool:
    """Check whether a profile is listed (not necessarily active) for a card."""
    res = _pactl("list", "cards")
    if res.returncode != 0:
        return False
    in_card = False
    in_profiles = False
    profile_re = re.compile(rf"^\s+{re.escape(profile)}:")
    for line in res.stdout.splitlines():
        s = line.strip()
        if s.startswith("Name: "):
            in_card = (s == f"Name: {card}")
            in_profiles = False
            continue
        if not in_card:
            continue
        if s == "Profiles:":
            in_profiles = True
            continue
        if in_profiles:
            if not line.startswith("\t\t") and not line.startswith("    "):
                in_profiles = False
            elif profile_re.match(line):
                return True
    return False
