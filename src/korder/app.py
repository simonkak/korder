from __future__ import annotations
import sys
from PySide6.QtWidgets import QApplication

from korder import config
from korder.audio.capture import MicRecorder
from korder.transcribe.whisper_engine import WhisperEngine
from korder.inject import InjectError, make_backend
from korder.ui.main_window import MainWindow


def _bool(s: str) -> bool:
    return s.strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    cfg = config.load()

    app = QApplication(sys.argv)
    app.setApplicationName("Korder")
    app.setOrganizationDomain("local.korder")

    recorder = MicRecorder(
        sample_rate=int(cfg["audio"]["sample_rate"]),
        device=cfg["audio"]["device"] or None,
    )
    engine = WhisperEngine(
        model=cfg["whisper"]["model"],
        compute_type=cfg["whisper"]["compute_type"],
        language=cfg["whisper"]["language"] or None,
    )

    try:
        injector = make_backend()
    except InjectError as e:
        print(f"[korder] injection disabled: {e}", file=sys.stderr)
        injector = None

    window = MainWindow(
        engine=engine,
        recorder=recorder,
        injector=injector,
        stay_on_top=_bool(cfg["ui"]["stay_on_top"]),
        non_focusable=_bool(cfg["ui"]["non_focusable"]),
        trailing_space=_bool(cfg["inject"]["trailing_space"]),
    )
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
