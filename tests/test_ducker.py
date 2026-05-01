"""VolumeDucker tests with mocked subprocess.run. Verifies the
duck/restore lifecycle, idempotency, and graceful no-op paths so a
broken wpctl never blocks the recording session."""
from __future__ import annotations
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from korder.audio.ducker import VolumeDucker


class _FakeWpctl:
    """Pretend to be wpctl: holds a current sink volume, responds to
    get-volume/set-volume calls with the right text/return code."""

    def __init__(self, initial: float = 0.80):
        self.volume = initial
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        if cmd[0] != "wpctl":
            raise FileNotFoundError(cmd[0])
        verb = cmd[1]
        if verb == "get-volume":
            return _completed(stdout=f"Volume: {self.volume:.2f}\n")
        if verb == "set-volume":
            self.volume = float(cmd[3])
            return _completed(stdout="")
        raise AssertionError(f"unexpected wpctl verb {verb!r}")


def _completed(stdout: str = "", returncode: int = 0):
    cp = subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")
    return cp


def test_disabled_ducker_does_not_call_wpctl():
    fake = _FakeWpctl(initial=0.80)
    with patch("korder.audio.ducker.subprocess.run", side_effect=fake):
        d = VolumeDucker(enabled=False, target_pct=30)
        d.duck()
        d.restore()
    assert fake.calls == []
    assert fake.volume == 0.80  # untouched


def test_duck_lowers_volume_and_restore_brings_it_back():
    fake = _FakeWpctl(initial=0.80)
    with patch("korder.audio.ducker.subprocess.run", side_effect=fake):
        d = VolumeDucker(enabled=True, target_pct=30)
        d.duck()
        assert fake.volume == pytest.approx(0.30, abs=1e-6)
        d.restore()
        assert fake.volume == pytest.approx(0.80, abs=1e-6)


def test_duck_is_idempotent_preserves_original_level():
    """A second duck() call while already ducked must NOT replace the
    saved original with the already-lowered value, or restore would
    leave the volume permanently low."""
    fake = _FakeWpctl(initial=0.75)
    with patch("korder.audio.ducker.subprocess.run", side_effect=fake):
        d = VolumeDucker(enabled=True, target_pct=20)
        d.duck()
        # Pretend the user toggles recording off then on quickly without
        # the restore landing first — second duck() should be a no-op.
        d.duck()
        d.restore()
    # Volume back to original 0.75, not stuck at 0.20.
    assert fake.volume == pytest.approx(0.75, abs=1e-6)


def test_restore_without_duck_is_noop():
    fake = _FakeWpctl(initial=0.50)
    with patch("korder.audio.ducker.subprocess.run", side_effect=fake):
        d = VolumeDucker(enabled=True, target_pct=30)
        d.restore()  # never ducked → should not touch volume
    # Only allowed call would be the get-volume from a duck() that didn't happen
    assert fake.volume == 0.50
    set_calls = [c for c in fake.calls if len(c) > 1 and c[1] == "set-volume"]
    assert set_calls == []


def test_duck_skips_when_already_below_target():
    """If the user is already quieter than the duck target, ducking
    *up* would be backwards — no-op instead."""
    fake = _FakeWpctl(initial=0.10)
    with patch("korder.audio.ducker.subprocess.run", side_effect=fake):
        d = VolumeDucker(enabled=True, target_pct=30)
        d.duck()
        d.restore()
    assert fake.volume == pytest.approx(0.10, abs=1e-6)
    # No set-volume call should have happened
    set_calls = [c for c in fake.calls if len(c) > 1 and c[1] == "set-volume"]
    assert set_calls == []


def test_duck_handles_missing_wpctl_gracefully():
    def raise_fnf(*_args, **_kwargs):
        raise FileNotFoundError("wpctl")

    with patch("korder.audio.ducker.subprocess.run", side_effect=raise_fnf):
        d = VolumeDucker(enabled=True, target_pct=30)
        # Must not raise — recording must continue even without wpctl
        d.duck()
        d.restore()


def test_duck_handles_get_volume_garbage_output():
    """If wpctl's output format changes / it errors with weird text,
    the parse fails cleanly and ducking is skipped."""
    def garbage(*_args, **_kwargs):
        return _completed(stdout="something unexpected\n")

    with patch("korder.audio.ducker.subprocess.run", side_effect=garbage):
        d = VolumeDucker(enabled=True, target_pct=30)
        d.duck()  # parse fails → no-op, no exception
        d.restore()


def test_duck_handles_set_volume_failure():
    """get-volume succeeds, set-volume raises — saved state must NOT be
    populated, so a subsequent restore() doesn't try to set anything."""
    state = {"calls": 0}

    def handler(cmd, *args, **kwargs):
        state["calls"] += 1
        if cmd[1] == "get-volume":
            return _completed(stdout="Volume: 0.80\n")
        # set-volume fails
        raise subprocess.CalledProcessError(returncode=1, cmd=cmd)

    with patch("korder.audio.ducker.subprocess.run", side_effect=handler):
        d = VolumeDucker(enabled=True, target_pct=30)
        d.duck()
        # _saved should be None because set failed; restore is a no-op
        assert d._saved is None
        d.restore()


def test_target_pct_clamped_to_valid_range():
    fake = _FakeWpctl(initial=0.80)
    with patch("korder.audio.ducker.subprocess.run", side_effect=fake):
        # 200% clamps to 100, so target=1.0 — and 1.0 > 0.80 so it's
        # actually a no-op (already-below-target check). Just verify
        # construction doesn't blow up and over-target doesn't raise.
        d = VolumeDucker(enabled=True, target_pct=200)
        assert d._target == 1.0
        d.duck()  # no-op since current 0.80 <= target 1.0
    fake = _FakeWpctl(initial=0.50)
    with patch("korder.audio.ducker.subprocess.run", side_effect=fake):
        d = VolumeDucker(enabled=True, target_pct=-10)
        assert d._target == 0.0
        d.duck()
        assert fake.volume == pytest.approx(0.0, abs=1e-6)
