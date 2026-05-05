"""D-Bus service Korder hosts so KWin scripts can return data to us.

KWin's JS scripting API doesn't have file I/O, but it has `callDBus()`
for outbound D-Bus calls. By hosting `org.korder.KwinBridge` on the
session bus and exposing slots for the data shapes scripts need to
return, we can request a fresh window list and synchronously receive
it from JS — without polling journald or registering full D-Bus
services per-call.

Lifecycle:
- `init()` is called once at app startup, AFTER QApplication exists
  (the QtDBus connection requires a Qt event loop).
- `list_windows()` is the synchronous public API. It triggers a KWin
  script that callDBus's the result back, then waits on a
  threading.Event for the slot to fire.
- The slot runs on the Qt main thread (Qt routes cross-thread D-Bus
  calls via queued connections); the caller is typically on the
  inject worker thread, so the wait crosses threads — fine because
  threading.Event is thread-safe and the Qt event loop processes
  D-Bus events independently of the worker thread's wait.

Fail-soft: if the bus isn't connected or service registration fails
(another Korder instance, broken session, etc.), `list_windows()`
returns an empty list and the LLM prompt simply omits the
`Current windows:` block. Voice flow still works without it."""
from __future__ import annotations
import json
import logging
import threading
from typing import Optional

from PySide6.QtCore import QObject, Slot
from PySide6.QtDBus import QDBusConnection

log = logging.getLogger(__name__)

_SERVICE = "org.korder.KwinBridge"
_PATH = "/"
_INTERFACE = "org.korder.KwinBridge"


class _BridgeReceiver(QObject):
    """QObject hosting the slots KWin scripts call into. Kept minimal
    — one slot per data shape so QtDBus introspection stays clean."""

    def __init__(self, on_window_list):
        super().__init__()
        self._on_window_list = on_window_list

    @Slot(str)
    def windowList(self, payload: str) -> None:
        try:
            self._on_window_list(payload)
        except Exception as e:
            log.warning("kwin_bridge: windowList callback failed: %s", e)


class _Bridge:
    def __init__(self) -> None:
        self._latest: str = ""
        self._event = threading.Event()
        # Serialize concurrent list_windows() calls so two simultaneous
        # KWin script triggers don't race on the latest payload.
        self._call_lock = threading.Lock()
        self._receiver: Optional[_BridgeReceiver] = None
        self._connected = False

        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            log.warning("kwin_bridge: session bus not connected")
            return
        if not bus.registerService(_SERVICE):
            log.warning(
                "kwin_bridge: registerService(%s) failed — another Korder "
                "instance may already own this name",
                _SERVICE,
            )
            return
        self._receiver = _BridgeReceiver(self._on_payload)
        if not bus.registerObject(
            _PATH,
            _INTERFACE,
            self._receiver,
            QDBusConnection.RegisterOption.ExportAllSlots,
        ):
            log.warning("kwin_bridge: registerObject failed")
            bus.unregisterService(_SERVICE)
            return
        self._connected = True
        log.info("kwin_bridge: registered %s on session bus", _SERVICE)

    def _on_payload(self, payload: str) -> None:
        self._latest = payload
        self._event.set()

    def list_windows(self, trigger_fn, timeout_s: float = 1.0) -> list[dict]:
        if not self._connected:
            return []
        with self._call_lock:
            self._event.clear()
            self._latest = ""
            try:
                if not trigger_fn():
                    return []
            except Exception as e:
                log.warning("kwin_bridge: trigger script failed: %s", e)
                return []
            if not self._event.wait(timeout_s):
                log.warning(
                    "kwin_bridge: timed out after %.1fs waiting for window list",
                    timeout_s,
                )
                return []
            try:
                data = json.loads(self._latest)
                if isinstance(data, list):
                    return data
            except Exception as e:
                log.warning("kwin_bridge: parse failed: %s", e)
        return []


_BRIDGE: Optional[_Bridge] = None
_INIT_LOCK = threading.Lock()


def init() -> None:
    """Idempotently create the global bridge instance. Must be called
    after QApplication is constructed — QDBusConnection needs the Qt
    event loop. Safe to call repeatedly; no-op on subsequent calls."""
    global _BRIDGE
    with _INIT_LOCK:
        if _BRIDGE is not None:
            return
        _BRIDGE = _Bridge()


def list_windows(timeout_s: float = 1.0) -> list[dict]:
    """Synchronously fetch the current window list. Returns [] on any
    failure — bridge not initialized, KWin not running, script load
    failed, callback timeout. Each entry is a dict with keys:
    `id`, `caption`, `resourceClass`, `minimized`."""
    if _BRIDGE is None:
        return []
    # Imported lazily so test code can mock this module without
    # pulling in the kwin module's subprocess machinery.
    from korder import kwin

    js = """
    (function() {
        const out = [];
        workspace.windowList().forEach(w => {
            if (w.normalWindow && !w.skipTaskbar) {
                out.push({
                    id: w.internalId.toString(),
                    caption: w.caption || "",
                    resourceClass: w.resourceClass || "",
                    minimized: !!w.minimized
                });
            }
        });
        callDBus(
            "org.korder.KwinBridge", "/", "org.korder.KwinBridge",
            "windowList", JSON.stringify(out)
        );
    })();
    """
    return _BRIDGE.list_windows(
        lambda: kwin._run_script(js), timeout_s=timeout_s
    )


def _set_bridge_for_test(bridge: Optional[_Bridge]) -> None:
    """Test hook: swap the global bridge for a stub. None resets."""
    global _BRIDGE
    _BRIDGE = bridge


def is_available() -> bool:
    """True iff the bridge is registered and ready to receive
    callbacks. Used by the prompt builder to decide whether to do a
    window-list fetch in the first place."""
    return _BRIDGE is not None and _BRIDGE._connected
