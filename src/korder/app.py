from __future__ import annotations
import argparse
import os
import socket
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QAction, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from korder import config
from korder.audio.capture import MicRecorder
from korder.transcribe.whisper_engine import WhisperEngine
from korder.inject import InjectError, make_backend
from korder.ui.main_window import MainWindow
from korder.ui.osd import OSDWindow
from korder.ui.settings_dialog import SettingsDialog

SOCKET_NAME = f"korder-{os.getuid()}"
SOCKET_PATH = os.path.join(tempfile.gettempdir(), SOCKET_NAME)
VALID_COMMANDS = {"toggle", "show"}


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
    if cfg["inject"]["action_parser"].lower() == "llm":
        from korder.intent import IntentParser
        thinking = _bool(cfg["intent"]["thinking_mode"])
        show_triggers = _bool(cfg["intent"]["show_triggers_in_prompt"])
        op_parser = IntentParser(
            model=cfg["inject"]["llm_model"],
            thinking_mode=thinking,
            show_triggers_in_prompt=show_triggers,
        ).parse
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
        injector = make_backend(paste_mode=cfg["inject"]["paste_mode"], op_parser=op_parser)
    except InjectError as e:
        print(f"[korder] injection disabled: {e}", file=sys.stderr)
        injector = None

    osd = OSDWindow()
    osd.map_offscreen()

    window = MainWindow(
        engine=engine,
        recorder=recorder,
        injector=injector,
        osd=osd,
        trailing_space=_bool(cfg["inject"]["trailing_space"]),
        auto_stop_after_action=_bool(cfg["ui"]["auto_stop_after_action"]),
    )

    tray = _make_tray(window)
    tray.show()

    server = _start_ipc_server(window)

    bt_workers = _start_bluetooth(cfg, window, recorder, tray)

    def _on_quit() -> None:
        for w in bt_workers:
            try:
                w.stop()
            except Exception:
                pass
        server.close()
        window.shutdown()
        osd.hide_now()
        tray.hide()

    app.aboutToQuit.connect(_on_quit)

    return app.exec()


def _start_bluetooth(cfg, window: MainWindow, recorder: MicRecorder, tray: QSystemTrayIcon) -> list:
    """Wire up the optional Bluetooth-headphone integration.

    When `[bluetooth] enabled = true` and a `device_mac` is set, start two
    background threads:

      - BluezPresenceWatcher — toggles which mic the recorder uses based
        on whether the configured headphones are connected.
      - AmaButtonListener — connects to the AMA RFCOMM service whenever
        the headphones are connected, fires `window.toggle_recording`
        on each Alexa-button press.
    """
    bt = cfg["bluetooth"]
    if not _bool(bt.get("enabled", "false")) or not bt.get("device_mac", ""):
        return []

    from korder.triggers.ama_button import AmaButtonListener
    from korder.triggers.bt_presence import BluezPresenceWatcher
    from korder.audio.source_router import (
        bt_card_name,
        bt_input_source_for,
        get_active_profile,
        set_profile,
    )

    mac = bt["device_mac"].strip()
    channel = int(bt.get("ama_channel", "19"))
    hfp_profile = bt.get("hfp_profile", "headset-head-unit")
    switch_profile = _bool(bt.get("switch_profile_for_recording", "true"))
    desk_device = recorder.device  # remember to fall back when BT goes away
    card = bt_card_name(mac)

    presence = BluezPresenceWatcher(mac)
    ama = AmaButtonListener(mac, channel=channel)

    # Profile-switch hooks: only attach while the headphones are connected.
    # Stored on the recorder so MicRecorder.start()/stop() invokes them.
    pre_state = {"original_profile": None}

    def _pre_start() -> None:
        if not switch_profile:
            return
        pre_state["original_profile"] = get_active_profile(card)
        if pre_state["original_profile"] != hfp_profile:
            set_profile(card, hfp_profile)

    def _post_stop() -> None:
        if not switch_profile:
            return
        orig = pre_state.get("original_profile")
        if orig and orig != hfp_profile:
            set_profile(card, orig)

    def _on_connected() -> None:
        try:
            recorder.set_device(bt_input_source_for(mac))
        except RuntimeError:
            # Recording in progress — leave the device alone; next start() picks up.
            pass
        recorder.pre_start = _pre_start
        recorder.post_stop = _post_stop
        tray.setToolTip(f"Korder — using BT mic ({mac[-8:]})")
        if not ama.isRunning():
            ama.start()

    def _on_disconnected() -> None:
        ama.stop()
        recorder.pre_start = None
        recorder.post_stop = None
        try:
            recorder.set_device(desk_device)
        except RuntimeError:
            pass
        tray.setToolTip("Korder — voice transcription")

    presence.connected.connect(_on_connected)
    presence.disconnected.connect(_on_disconnected)
    ama.pressed.connect(window.toggle_recording)
    presence.start()

    print(
        f"[korder] bluetooth integration on for {mac} (AMA ch {channel})",
        file=sys.stderr,
    )
    return [presence, ama]


def _make_tray(window: MainWindow) -> QSystemTrayIcon:
    tray = QSystemTrayIcon(_tray_icon())
    tray.setToolTip("Korder — voice transcription")

    menu = QMenu()

    act_toggle = QAction("Toggle recording", menu)
    act_toggle.triggered.connect(window.toggle_recording)
    menu.addAction(act_toggle)

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

    def _on_activated(reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            window.toggle_recording()

    tray.activated.connect(_on_activated)
    return tray


def _show_settings(parent: MainWindow) -> None:
    dlg = SettingsDialog(parent)
    dlg.settings_saved.connect(lambda: parent.statusBar().showMessage(
        "Settings saved — restart Korder for all changes to take effect.", 8000
    ))
    dlg.exec()


_TRAY_SVG = Path(__file__).resolve().parent / "ui" / "icons" / "tray.svg"
_TRAY_RENDER_SIZES = (16, 22, 24, 32, 48, 64)


def _tray_icon() -> QIcon:
    """Bundled waveform tray icon, recolored to the active theme.

    The SVG paints with ``currentColor``, which Qt's icon engine doesn't
    substitute on its own. We render to a transparent pixmap, then composite
    the foreground color in via ``CompositionMode_SourceIn`` — that recolors
    the alpha-shaped glyph without touching the SVG source.
    """
    renderer = QSvgRenderer(str(_TRAY_SVG))
    fg = QGuiApplication.palette().windowText().color()
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


if __name__ == "__main__":
    sys.exit(main())
