from __future__ import annotations
import argparse
import logging
import os
import socket
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# CRITICAL: rebrand the process and set audio-server identity env vars
# BEFORE any import that transitively loads sounddevice/PortAudio.
#
# PipeWire's pw_get_prgname() (used to label clients in the volume
# mixer) reads glibc's program_invocation_short_name, which is set at
# exec() from argv[0] and is NOT updated by prctl(PR_SET_NAME) alone.
# setproctitle rewrites both argv[0] in memory AND glibc's pointer, so
# pipewire then sees "korder" instead of "python3.12". This is the
# only way to rename the *persistent* PipeWire client (factory.id=2)
# that PortAudio creates the moment its ALSA hostapi initializes.
#
# Failure here is non-fatal — without setproctitle the mixer just
# falls back to the previous "ALSA plug-in [python3.12]" labelling.
try:
    import setproctitle as _setproctitle
    _setproctitle.setproctitle("korder")
except ImportError:
    pass

# PipeWire reads three different env vars for three different objects
# we touch when we record. All three need to be set before sounddevice
# imports — the audio libs read them at connect time, and `setdefault`
# leaves any user override intact.
#
# - PIPEWIRE_PROPS: properties for libpipewire's primary client, the
#   one PortAudio creates the moment its ALSA hostapi initializes.
#   Format is SPA-JSON (PipeWire's relaxed-JSON dialect): braces,
#   key=value pairs, **comma-separated**. Earlier attempts without
#   commas were silently ignored or, in the worst case, hung pw_init.
# - PIPEWIRE_ALSA: properties for the per-stream node that
#   pcm_pipewire creates when an InputStream actually opens. Same
#   SPA-JSON format.
# - PULSE_PROP: covers users on plain PulseAudio (libpulse format —
#   space-separated key=value, no braces).
#
# `application.id` and `application.process.binary` are how Plasma's
# volume mixer looks up the matching .desktop file (installed below
# in _install_desktop_entry). Without them — even with application.name
# set — some mixer versions display the auto-derived "ALSA plug-in
# [python3.12]" instead, because they prefer the .desktop-resolved
# label over the raw stream property when neither application.id nor
# process.binary point at a registered application.
_PW_PROPS = (
    "application.name = Korder, "
    "application.icon_name = korder, "
    "application.id = korder, "
    "application.process.binary = korder"
)
os.environ.setdefault("PIPEWIRE_PROPS", "{ " + _PW_PROPS + " }")
os.environ.setdefault("PIPEWIRE_ALSA", "{ " + _PW_PROPS + ", node.name = Korder }")
os.environ.setdefault(
    "PULSE_PROP",
    "application.name=Korder "
    "application.icon_name=korder "
    "application.id=korder "
    "application.process.binary=korder",
)

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
from korder.ui.i18n import t
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


def _setup_logging() -> None:
    """Wire Python's logging to stderr so modules using
    logging.getLogger() are actually visible. Otherwise INFO/DEBUG
    messages silently route through Python's lastResort handler
    (WARNING+ only). Honors KORDER_LOG_LEVEL (DEBUG/INFO/WARNING/
    ERROR; default INFO). Format mirrors the legacy `[korder] message`
    convention so the new logger output reads alongside historical
    print statements.

    Third-party loggers that are chatty at INFO (pywhispercpp's
    per-segment "Transcribing... / Inference time: 0.18s" lines,
    PIL's stream-debug spam) are clamped to WARNING when our level
    is above DEBUG — they show up at DEBUG only. Stops them from
    drowning out korder's own INFO output by default while keeping
    everything visible when actually debugging.

    Korder's modules use logging.getLogger() throughout; without
    this setup, INFO/DEBUG diagnostics route through Python's
    lastResort handler (WARNING+ only) and are silent."""
    level_name = (os.environ.get("KORDER_LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=level,
            format="[%(name)s] %(message)s",
            stream=sys.stderr,
        )
    logging.getLogger("korder").setLevel(level)
    # Quiet down chatty third-party loggers that emit per-segment /
    # per-frame INFO messages. Restored to default at DEBUG so they
    # remain available for actual debugging. Names enumerated
    # explicitly rather than catch-all so we don't accidentally
    # suppress something useful from a future dependency.
    if level > logging.DEBUG:
        for noisy in ("pywhispercpp", "pywhispercpp.model", "pywhispercpp.utils", "PIL"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    _setup_logging()
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
        log.error("not running; start with `korder` first")
        return 1

    return _run_app()


def _run_app() -> int:
    cfg = config.load()

    # Audio-server identity env vars are set at module-import time
    # (see top of this file — they have to land before sounddevice
    # imports). Here we install the side artifacts the env vars
    # reference: an icon file (so application.icon_name resolves) and
    # a .desktop entry (so application.id resolves to a real
    # registered app, which is what the mixer's lookup actually wants).
    _install_tray_icon_in_theme()
    _install_desktop_entry()

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
    op_parser_last_response = None
    op_parser_clear_history = None
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
        # Read the LLM's natural-language reply from the most recent
        # parse. Used today for confirmation prompts on destructive
        # actions; replaces PR #6's separate-call generation +
        # boot-time cache warming.
        op_parser_last_response = lambda: intent_parser.last_response
        # Drop conversation history at session boundaries (mic close /
        # cancel). Scopes follow-up resolution to a single dictation
        # invocation — see IntentParser.clear_history.
        op_parser_clear_history = intent_parser.clear_history
        flags = []
        if thinking:
            flags.append("thinking")
        if show_triggers:
            flags.append("show-triggers")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        log.info("using LLM action parser (%s)%s", cfg['inject']['llm_model'], flag_str)

    try:
        injector = make_backend(
            paste_mode=cfg["inject"]["paste_mode"],
            op_parser=op_parser,
            op_parser_is_warm=op_parser_is_warm,
            op_parser_warm_up=op_parser_warm_up,
            op_parser_last_response=op_parser_last_response,
            op_parser_clear_history=op_parser_clear_history,
        )
    except InjectError as e:
        log.error("injection disabled: %s", e)
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

    tts_engine = _build_tts_engine(cfg) if _bool(cfg["tts"]["enabled"]) else None

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
        tts=tts_engine,
        tts_suppress_when_playing=_bool(cfg["tts"]["suppress_when_playing"]),
        tts_speak_action_progress=_bool(cfg["tts"]["speak_action_progress"]),
        start_chime=_bool(cfg["audio"]["start_chime"]),
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
        if tts_engine is not None:
            tts_engine.shutdown()
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
        log.error("wake: detector init failed: %s", e)
        return None
    return detector


def _build_tts_engine(cfg):
    """Construct the SpeechEngine if [tts] is enabled. Failures are
    logged and return None so the app boots in text-only mode rather
    than crashing — Piper not being installed is a configuration
    issue, not a fatal error."""
    from korder.audio.tts import SpeechEngine

    try:
        speed = float(cfg["tts"]["speed"])
    except (KeyError, ValueError):
        speed = 1.0
    try:
        engine = SpeechEngine(
            enabled=True,
            voice_en=cfg["tts"]["voice_en"].strip() or "en_US-amy-medium",
            voice_pl=cfg["tts"]["voice_pl"].strip() or "pl_PL-darkman-medium",
            speed=speed,
        )
    except Exception as e:
        log.error("tts: engine init failed: %s", e)
        return None
    return engine


def _make_tray(window: MainWindow) -> QSystemTrayIcon:
    icons = {state: _tray_icon(state) for state in ("idle", "wake_listening", "dictating")}
    tooltips = {
        "idle": t("tray_tooltip_idle"),
        "wake_listening": t("tray_tooltip_wake_listening"),
        "dictating": t("tray_tooltip_dictating"),
    }

    tray = QSystemTrayIcon(icons["idle"])
    tray.setToolTip(tooltips["idle"])

    menu = QMenu()

    act_toggle = QAction(t("menu_toggle_recording"), menu)
    act_toggle.triggered.connect(window.toggle_recording)
    menu.addAction(act_toggle)

    act_wake = QAction(t("menu_wake_listening"), menu)
    act_wake.setCheckable(True)
    act_wake.toggled.connect(
        lambda on: window.start_wake_listening() if on else window.stop_wake_listening()
    )
    menu.addAction(act_wake)

    act_history = QAction(t("menu_show_history"), menu)
    act_history.triggered.connect(lambda: (window.show(), window.raise_(), window.activateWindow()))
    menu.addAction(act_history)

    act_settings = QAction(t("menu_settings"), menu)
    act_settings.triggered.connect(lambda: _show_settings(window))
    menu.addAction(act_settings)

    menu.addSeparator()

    act_quit = QAction(t("menu_quit"), menu)
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
    dlg.settings_saved.connect(
        lambda: parent.statusBar().showMessage(t("settings_saved_notice"), 8000)
    )
    dlg.exec()


_TRAY_SVG = Path(__file__).resolve().parent / "ui" / "icons" / "tray.svg"
_DESKTOP_SRC = Path(__file__).resolve().parent / "ui" / "icons" / "korder.desktop"

# Where freedesktop's icon spec wants per-user app icons. Plasma's
# volume mixer (and most GTK/Qt apps) look here when resolving an
# `application.icon_name` like the one we set in PULSE_PROP below.
_USER_ICON_PATH = Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps" / "korder.svg"
# Where the freedesktop spec wants per-user .desktop entries. The
# volume mixer matches a stream's application.id against the basename
# of files here (without ".desktop") to find display name + icon.
_USER_DESKTOP_PATH = Path.home() / ".local" / "share" / "applications" / "korder.desktop"


def _install_tray_icon_in_theme() -> None:
    """Install the bundled tray SVG into the user's hicolor icon theme
    so application.icon_name=korder (set in PIPEWIRE_ALSA / PULSE_PROP
    at the top of this module) resolves to a real glyph. No-op if the
    file is already there. Failure is non-fatal — the stream still
    gets named, it just falls back to a default mic icon."""
    try:
        if not _USER_ICON_PATH.exists() and _TRAY_SVG.exists():
            _USER_ICON_PATH.parent.mkdir(parents=True, exist_ok=True)
            _USER_ICON_PATH.write_bytes(_TRAY_SVG.read_bytes())
    except OSError as e:
        log.error("couldn't install tray icon: %s", e)


def _install_desktop_entry() -> None:
    """Install ~/.local/share/applications/korder.desktop so KDE's
    volume mixer + app launcher + window-grouping logic can match
    application.id=korder (set in PIPEWIRE_PROPS et al.) to a real
    application metadata entry. The file is the canonical answer to
    "what is this stream and how should I render it?" — without it,
    the mixer falls back to its auto-generated label.

    Idempotent on every launch — only writes if the destination is
    missing, never overwrites a user customization. Always rewrites
    when the bundled source has changed (different bytes), so package
    updates with edited metadata land on next launch."""
    try:
        if not _DESKTOP_SRC.exists():
            return
        src_bytes = _DESKTOP_SRC.read_bytes()
        if _USER_DESKTOP_PATH.exists():
            if _USER_DESKTOP_PATH.read_bytes() == src_bytes:
                return
        _USER_DESKTOP_PATH.parent.mkdir(parents=True, exist_ok=True)
        _USER_DESKTOP_PATH.write_bytes(src_bytes)
    except OSError as e:
        log.error("couldn't install desktop entry: %s", e)
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
            log.error("IPC server failed: %s", server.errorString())

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
