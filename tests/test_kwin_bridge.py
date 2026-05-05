"""Tests for the KWin → Korder D-Bus bridge that delivers the
window list to the LLM prompt builder. These don't exercise the
real session bus — they swap a stub bridge into the module's
slot and verify the public list_windows() contract.

The QtDBus wiring itself (registerService, registerObject,
@Slot delivery) is exercised at runtime when app.py runs against
a live session; covering it in unit tests would require either
a real bus or a heavyweight QtDBus mock and the cost-benefit
isn't there for code that's a thin wrapper around Qt's API."""
from __future__ import annotations
import json
from unittest.mock import patch

from korder import kwin_bridge


class _StubBridge:
    """Stand-in for the real `_Bridge` whose only job is to capture
    `trigger_fn` invocation and return a configured payload."""

    def __init__(self, payload: list[dict] | None = None, simulate_fail: bool = False):
        self._payload = payload or []
        self._simulate_fail = simulate_fail
        self.trigger_calls: int = 0
        self._connected = True

    def list_windows(self, trigger_fn, timeout_s: float = 1.0) -> list[dict]:
        if self._simulate_fail:
            return []
        # Mimic the real bridge: trigger_fn fires the script, then we
        # "receive" the payload — the stub skips the actual D-Bus
        # round-trip and returns directly.
        try:
            trigger_fn()
        except Exception:
            return []
        self.trigger_calls += 1
        return list(self._payload)


def test_list_windows_returns_empty_when_bridge_uninitialized():
    """Without init(), the public helper must degrade — voice flow
    on non-Plasma / non-Wayland systems can't call this and shouldn't
    crash the prompt builder."""
    kwin_bridge._set_bridge_for_test(None)
    try:
        assert kwin_bridge.list_windows() == []
    finally:
        kwin_bridge._set_bridge_for_test(None)


def test_list_windows_returns_payload_from_bridge():
    sample = [
        {"id": "uuid-1", "caption": "PR #27", "resourceClass": "firefox", "minimized": False},
        {"id": "uuid-2", "caption": "Korder", "resourceClass": "konsole", "minimized": False},
    ]
    stub = _StubBridge(payload=sample)
    kwin_bridge._set_bridge_for_test(stub)
    try:
        with patch("korder.kwin._run_script", return_value=True):
            got = kwin_bridge.list_windows(timeout_s=0.1)
        assert got == sample
        assert stub.trigger_calls == 1
    finally:
        kwin_bridge._set_bridge_for_test(None)


def test_list_windows_returns_empty_when_bridge_fails():
    """Timeout / parse failure / disconnected bus all surface as []
    so the prompt builder simply omits the window block."""
    stub = _StubBridge(simulate_fail=True)
    kwin_bridge._set_bridge_for_test(stub)
    try:
        assert kwin_bridge.list_windows() == []
    finally:
        kwin_bridge._set_bridge_for_test(None)


def test_list_windows_triggers_callDBus_in_script_body():
    """Smoke-check the script body: it must callDBus into our service
    so the bridge's slot fires."""
    stub = _StubBridge(payload=[])
    kwin_bridge._set_bridge_for_test(stub)
    captured = {"body": ""}

    def fake_run_script(js: str) -> bool:
        captured["body"] = js
        return True

    try:
        with patch("korder.kwin._run_script", side_effect=fake_run_script):
            kwin_bridge.list_windows()
    finally:
        kwin_bridge._set_bridge_for_test(None)

    assert "callDBus" in captured["body"]
    assert "org.korder.KwinBridge" in captured["body"]
    assert "windowList" in captured["body"]
    assert "workspace.windowList" in captured["body"]


def test_is_available_reflects_bridge_state():
    kwin_bridge._set_bridge_for_test(None)
    assert kwin_bridge.is_available() is False
    kwin_bridge._set_bridge_for_test(_StubBridge(payload=[]))
    try:
        assert kwin_bridge.is_available() is True
    finally:
        kwin_bridge._set_bridge_for_test(None)


def test_internal_bridge_payload_round_trip():
    """The real bridge serializes incoming JSON via the slot, decodes
    it with json.loads, returns a list. This exercises the parse
    path with a directly-driven `_Bridge`-shaped object."""
    # We can't construct the real _Bridge without a Qt event loop,
    # but we can call its _on_payload + list_windows methods to
    # verify the JSON parse contract.
    real = kwin_bridge._Bridge.__new__(kwin_bridge._Bridge)
    real._latest = ""
    import threading
    real._event = threading.Event()
    real._call_lock = threading.Lock()
    real._receiver = None
    real._connected = True

    def trigger():
        # Simulate KWin script firing the callback synchronously.
        real._on_payload(json.dumps([{"id": "x", "caption": "y", "resourceClass": "z", "minimized": False}]))
        return True

    out = real.list_windows(trigger, timeout_s=0.5)
    assert out == [{"id": "x", "caption": "y", "resourceClass": "z", "minimized": False}]


def test_internal_bridge_returns_empty_on_invalid_json():
    real = kwin_bridge._Bridge.__new__(kwin_bridge._Bridge)
    real._latest = ""
    import threading
    real._event = threading.Event()
    real._call_lock = threading.Lock()
    real._receiver = None
    real._connected = True

    def trigger():
        real._on_payload("not valid json {{{")
        return True

    assert real.list_windows(trigger, timeout_s=0.5) == []


def test_internal_bridge_returns_empty_when_disconnected():
    real = kwin_bridge._Bridge.__new__(kwin_bridge._Bridge)
    real._connected = False
    assert real.list_windows(lambda: True, timeout_s=0.5) == []
