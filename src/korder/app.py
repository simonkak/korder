from __future__ import annotations
import argparse
import os
import socket
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QAction, QColor, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from korder import config
from korder.audio.capture import MicRecorder
from korder.audio.ducker import VolumeDucker
from korder.transcribe.whisper_engine import WhisperEngine
from korder.inject import InjectError, make_backend
from korder.ui.main_window import MainWindow
from korder.ui.osd import OSDWindow
from korder.ui.settings_dialog import SettingsDialog

SOCKET_NAME = f"korder-{os.getuid()}"
SOCKET_PATH = os.path.join(tempfile.gettempdir(), SOCKET_NAME)
VALID_COMMANDS = {"toggle", "show", "cancel", "wake-toggle", "wake-on", "wake-off"}


def _bool(s: str) -> bool:
    return s.strip().lower() in {"1", "true", "yes", "on"}


def _try_forward(cmd: str, timeout_s: float = 0.5) -> bool:
    """Send a command to a running instance via its Unix socket. Returns True on success."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    try:
        s.connect(SOCKET_PATH)
        s.sendall(cmd.encode("utf-8") + b"\n")
        return True
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(prog="korder")
    parser.add_argument(
        "cmd",
        nargs="?",
        choices=sorted(VALID_COMMANDS),
        help="Send a control command to a running instance instead of launching.",
    )
    args = parser.parse_args()

    forward_cmd = args.cmd or "show"
    if _try_forward(forward_cmd):
        return 0
    if args.cmd is not None:
        print(f"[korder] not running; start with `korder` first", file=sys.stderr)
        return 1

    return _run_app()


def _run_app() -> int:
    cfg = config.load()

    # Announce ourselves to PipeWire/PulseAudio before any audio code
    # starts a stream — the volume mixer otherwise labels us
    # "PipeWire ALSA [python3.12]" which is correct but unhelpful.
    # Must happen before MicRecorder constructs its first InputStream.
    _announce_pulse_identity()

    app = QApplication(sys.argv)
    app.setApplicationName("Korder")
    app.setOrganizationDomain("local.korder")
    app.setQuitOnLastWindowClosed(False)

    recorder = MicRecorder(
        sample_rate=int(cfg["audio"]["sample_rate"]),
        device=cfg["audio"]["device"] or None,
        gain=float(cfg["audio"]["gain"]),
    )
    engine = WhisperEngine(
        model=cfg["whisper"]["model"],
        language=cfg["whisper"]["language"] or None,
        initial_prompt=cfg["whisper"]["initial_prompt"] or None,
        n_threads=int(cfg["whisper"]["n_threads"]),
    )

    op_parser = None
    op_parser_is_warm = None
    op_parser_warm_up = None
    if cfg["inject"]["action_parser"].lower() == "llm":
        from korder.intent import IntentParser
        thinking = _bool(cfg["intent"]["thinking_mode"])
        show_triggers = _bool(cfg["intent"]["show_triggers_in_prompt"])
        try:
            timeout_s = float(cfg["intent"]["timeout_s"])
        except (KeyError, ValueError):
            timeout_s = 20.0
        try:
            keep_alive_s = float(cfg["intent"]["keep_alive_s"])
        except (KeyError, ValueError):
            keep_alive_s = 300.0
        intent_parser = IntentParser(
            model=cfg["inject"]["llm_model"],
            timeout_s=timeout_s,
            thinking_mode=thinking,
            show_triggers_in_prompt=show_triggers,
            keep_alive_s=keep_alive_s,
        )
        op_parser = intent_parser.parse
        op_parser_is_warm = intent_parser.is_model_loaded
        op_parser_warm_up = intent_parser.warm_up
        flags = []
        if thinking:
            flags.append("thinking")
        if show_triggers:
            flags.append("show-triggers")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        print(
            f"[korder] using LLM action parser ({cfg['inject']['llm_model']}){flag_str}",
            file=sys.stderr,
        )

    try:
        injector = make_backend(
            paste_mode=cfg["inject"]["paste_mode"],
            op_parser=op_parser,
            op_parser_is_warm=op_parser_is_warm,
            op_parser_warm_up=op_parser_warm_up,
        )
    except InjectError as e:
        print(f"[korder] injection disabled: {e}", file=sys.stderr)
        injector = None

    osd = OSDWindow()
    osd.map_offscreen()

    try:
        duck_pct = int(cfg["audio"]["duck_volume_pct"])
    except (KeyError, ValueError):
        duck_pct = 30
    ducker = VolumeDucker(
        enabled=_bool(cfg["audio"]["duck_during_recording"]),
        target_pct=duck_pct,
    )

    wake_detector = _build_wake_detector(cfg, recorder) if _bool(cfg["wake"]["enabled"]) else None

    try:
        wake_idle_timeout_s = float(cfg["wake"]["idle_timeout_s"])
    except (KeyError, ValueError):
        wake_idle_timeout_s = 5.0

    window = MainWindow(
        engine=engine,
        recorder=recorder,
        injector=injector,
        osd=osd,
        trailing_space=_bool(cfg["inject"]["trailing_space"]),
        auto_stop_after_action=_bool(cfg["ui"]["auto_stop_after_action"]),
        ducker=ducker,
        wake_detector=wake_detector,
        wake_idle_timeout_s=wake_idle_timeout_s,
    )

    tray = _make_tray(window)
    tray.show()

    # Auto-start wake-listening if configured. Done after tray is up so
    # the icon swap on transition has a target.
    if wake_detector is not None:
        window.start_wake_listening()

    server = _start_ipc_server(window)

    def _on_quit() -> None:
        server.close()
        window.shutdown()
        osd.hide_now()
        tray.hide()

    app.aboutToQuit.connect(_on_quit)

    return app.exec()


def _build_wake_detector(cfg, recorder: MicRecorder):
    """Construct the WakeWordDetector if [wake] is enabled and the
    optional 'wake' extra is installed. Failures are logged and return
    None so the app boots in hotkey-only mode rather than crashing."""
    from korder.audio.wake import WakeWordDetector

    phrase = cfg["wake"]["phrase"].strip() or "hey_jarvis"
    try:
        sensitivity = float(cfg["wake"]["sensitivity"])
    except (KeyError, ValueError):
        sensitivity = 0.5
    try:
        detector = WakeWordDetector(
            recorder=recorder,
            phrase=phrase,
            sensitivity=sensitivity,
            sample_rate=recorder.sample_rate,
        )
    except Exception as e:
        print(f"[korder] wake: detector init failed: {e}", file=sys.stderr)
        return None
    return detector


def _make_tray(window: MainWindow) -> QSystemTrayIcon:
    icons = {state: _tray_icon(state) for state in ("idle", "wake_listening", "dictating")}
    tooltips = {
        "idle": "Korder — voice transcription",
        "wake_listening": "Korder — listening for wake word",
        "dictating": "Korder — recording…",
    }

    tray = QSystemTrayIcon(icons["idle"])
    tray.setToolTip(tooltips["idle"])

    menu = QMenu()

    act_toggle = QAction("Toggle recording", menu)
    act_toggle.triggered.connect(window.toggle_recording)
    menu.addAction(act_toggle)

    act_wake = QAction("Wake-word listening", menu)
    act_wake.setCheckable(True)
    act_wake.toggled.connect(
        lambda on: window.start_wake_listening() if on else window.stop_wake_listening()
    )
    menu.addAction(act_wake)

    act_history = QAction("Show transcript history", menu)
    act_history.triggered.connect(lambda: (window.show(), window.raise_(), window.activateWindow()))
    menu.addAction(act_history)

    act_settings = QAction("Settings…", menu)
    act_settings.triggered.connect(lambda: _show_settings(window))
    menu.addAction(act_settings)

    menu.addSeparator()

    act_quit = QAction("Quit", menu)
    act_quit.triggered.connect(QApplication.instance().quit)
    menu.addAction(act_quit)

    tray.setContextMenu(menu)

    def _on_state(state: str) -> None:
        tray.setIcon(icons.get(state, icons["idle"]))
        tray.setToolTip(tooltips.get(state, tooltips["idle"]))
        # Reflect the wake-listening flag on the menu checkbox without
        # triggering its toggled signal back at us.
        with _block_signals(act_wake):
            act_wake.setChecked(state == "wake_listening")

    window.tray_state_changed.connect(_on_state)
    # Sync the initial menu state without waiting for a user transition.
    _on_state("wake_listening" if window.is_wake_listening() else "idle")

    def _on_activated(reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            window.toggle_recording()

    tray.activated.connect(_on_activated)
    return tray


class _block_signals:
    """Context manager: blockSignals(True) for the duration of the
    block so a programmatic setChecked() on a QAction doesn't echo
    back into the slot we just connected."""

    def __init__(self, obj):
        self._obj = obj
        self._was = False

    def __enter__(self):
        self._was = self._obj.signalsBlocked()
        self._obj.blockSignals(True)
        return self._obj

    def __exit__(self, *exc):
        self._obj.blockSignals(self._was)
        return False


def _show_settings(parent: MainWindow) -> None:
    dlg = SettingsDialog(parent)
    dlg.settings_saved.connect(lambda: parent.statusBar().showMessage(
        "Settings saved — restart Korder for all changes to take effect.", 8000
    ))
    dlg.exec()


_TRAY_SVG = Path(__file__).resolve().parent / "ui" / "icons" / "tray.svg"

# Where freedesktop's icon spec wants per-user app icons. Plasma's
# volume mixer (and most GTK/Qt apps) look here when resolving an
# `application.icon_name` like the one we set in PULSE_PROP below.
_USER_ICON_PATH = Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps" / "korder.svg"


def _announce_pulse_identity() -> None:
    """Set PULSE_PROP so PipeWire/PulseAudio labels our streams as
    "Korder" with our icon, instead of the default "PipeWire ALSA
    [python3.12]" derived from the binary name. Also installs the
    bundled tray SVG into the user's hicolor icon theme so the
    icon_name lookup resolves to something visual.

    Both env var + icon install are no-ops if already in place
    (idempotent on every launch). PULSE_PROP is read by the Pulse
    client library when the audio app first connects, so this MUST
    run before sounddevice opens any stream.
    """
    # Install our SVG into the user's icon theme if missing. Failure
    # here is non-fatal — the stream still gets renamed, it just
    # falls back to a default mic icon.
    try:
        if not _USER_ICON_PATH.exists() and _TRAY_SVG.exists():
            _USER_ICON_PATH.parent.mkdir(parents=True, exist_ok=True)
            _USER_ICON_PATH.write_bytes(_TRAY_SVG.read_bytes())
    except OSError as e:
        print(f"[korder] couldn't install tray icon: {e}", file=sys.stderr)

    # PULSE_PROP is space-separated key=value pairs. setdefault keeps
    # any user override (e.g. testing with a custom name) intact.
    os.environ.setdefault(
        "PULSE_PROP",
        "application.name=Korder application.icon_name=korder",
    )
_TRAY_RENDER_SIZES = (16, 22, 24, 32, 48, 64)
# Per-state foreground colors. idle uses the theme; the other two are
# fixed accents so the user gets a consistent visual cue regardless of
# light/dark theme. Soft blue mirrors the OSD's Loading state; warm
# accent mirrors the Thinking/recording pulse.
_TRAY_STATE_COLORS = {
    "wake_listening": QColor(0x66, 0xb3, 0xf2),
    "dictating": QColor(0xe6, 0x82, 0x5a),
}


def _tray_icon(state: str = "idle") -> QIcon:
    """Bundled waveform tray icon, recolored per state.

    The SVG paints with ``currentColor``, which Qt's icon engine doesn't
    substitute on its own. We render to a transparent pixmap, then composite
    the foreground color in via ``CompositionMode_SourceIn`` — that recolors
    the alpha-shaped glyph without touching the SVG source. Different
    foreground per state gives the user a quick visual confirmation that
    the mic is open (and why).
    """
    renderer = QSvgRenderer(str(_TRAY_SVG))
    fg = _TRAY_STATE_COLORS.get(state) or QGuiApplication.palette().windowText().color()
    icon = QIcon()
    for size in _TRAY_RENDER_SIZES:
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        renderer.render(p, QRectF(0, 0, size, size))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        p.fillRect(pix.rect(), fg)
        p.end()
        icon.addPixmap(pix)
    return icon


def _start_ipc_server(window: MainWindow) -> QLocalServer:
    server = QLocalServer()
    if not server.listen(SOCKET_NAME):
        QLocalServer.removeServer(SOCKET_NAME)
        if not server.listen(SOCKET_NAME):
            print(
                f"[korder] IPC server failed: {server.errorString()}",
                file=sys.stderr,
            )

    def _on_new_connection() -> None:
        sock = server.nextPendingConnection()
        if sock is None:
            return
        sock.readyRead.connect(lambda: _on_ready_read(sock, window))
        sock.disconnected.connect(sock.deleteLater)

    server.newConnection.connect(_on_new_connection)
    return server


def _on_ready_read(sock: QLocalSocket, window: MainWindow) -> None:
    while sock.canReadLine():
        raw: QByteArray = sock.readLine()
        line = bytes(raw).decode("utf-8", errors="replace").strip()
        if line == "toggle":
            window.toggle_recording()
        elif line == "show":
            window.show()
            window.raise_()
        elif line == "cancel":
            window.cancel_recording()
        elif line == "wake-toggle":
            window.toggle_wake_listening()
        elif line == "wake-on":
            window.start_wake_listening()
        elif line == "wake-off":
            window.stop_wake_listening()


if __name__ == "__main__":
    sys.exit(main())
