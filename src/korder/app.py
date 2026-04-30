from __future__ import annotations
import argparse
import os
import socket
import sys
import tempfile

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QAction, QColor, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
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

    def _on_quit() -> None:
        server.close()
        window.shutdown()
        osd.hide_now()
        tray.hide()

    app.aboutToQuit.connect(_on_quit)

    return app.exec()


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


def _tray_icon() -> QIcon:
    icon = QIcon.fromTheme("audio-input-microphone")
    if not icon.isNull():
        return icon
    icon = QIcon.fromTheme("microphone")
    if not icon.isNull():
        return icon
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setBrush(QColor(220, 220, 220))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(20, 8, 24, 36, 12, 12)
    p.setBrush(QColor(180, 180, 180))
    p.drawRoundedRect(14, 28, 36, 6, 3, 3)
    p.drawRoundedRect(30, 36, 4, 14, 2, 2)
    p.drawRoundedRect(20, 50, 24, 4, 2, 2)
    p.end()
    return QIcon(pix)


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
