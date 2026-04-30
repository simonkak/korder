"""Settings dialog — exposes every key in ~/.config/korderrc as form fields,
organized in tabs. Saves write back to disk; user is told to restart for the
changes to fully take effect."""
from __future__ import annotations
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from korder import config

_WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]
_LANGS = ["", "pl", "en", "de", "fr", "es", "it", "uk", "cs", "ru"]
_PASTE_MODES = ["auto", "always", "never"]
_PARSERS = ["regex", "llm"]
_SAMPLE_RATES = ["16000", "32000", "48000"]


class SettingsDialog(QDialog):
    settings_saved = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Korder Settings")
        self.resize(620, 560)
        self._cfg = config.load()
        self._build_ui()
        self._populate_from_config()

    # ---- UI construction --------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_audio_whisper_tab(), "Mic && Whisper")
        self._tabs.addTab(self._build_actions_tab(), "Actions && Output")
        self._tabs.addTab(self._build_spotify_tab(), "Spotify")
        self._tabs.addTab(self._build_general_tab(), "General")
        layout.addWidget(self._tabs)

        hint = QLabel(
            "Most settings take effect after restarting Korder."
        )
        hint.setStyleSheet("color: palette(mid); font-style: italic;")
        layout.addWidget(hint)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
        )
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        apply_btn = btns.button(QDialogButtonBox.StandardButton.Apply)
        apply_btn.clicked.connect(self._on_apply)
        layout.addWidget(btns)

    def _build_audio_whisper_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)

        # Audio group
        audio = QGroupBox("Microphone")
        af = QFormLayout(audio)
        self._gain = QDoubleSpinBox()
        self._gain.setRange(0.1, 2.0)
        self._gain.setSingleStep(0.05)
        self._gain.setDecimals(2)
        self._gain.setSuffix("×")
        self._gain.setToolTip("Software gain on captured audio. Lower if mic is too hot.")
        af.addRow("Mic gain:", self._gain)

        self._sample_rate = QComboBox()
        self._sample_rate.addItems(_SAMPLE_RATES)
        self._sample_rate.setToolTip("Whisper expects 16000.")
        af.addRow("Sample rate:", self._sample_rate)

        self._device = QComboBox()
        self._device.addItem("(system default)", "")
        self._populate_audio_devices()
        af.addRow("Input device:", self._device)
        outer.addWidget(audio)

        # Whisper group
        whisper = QGroupBox("Whisper")
        wf = QFormLayout(whisper)
        self._model = QComboBox()
        self._model.setEditable(True)
        self._model.addItems(_WHISPER_MODELS)
        self._model.setToolTip("Bigger = better accuracy + slower. medium is the sweet spot for Polish.")
        wf.addRow("Model:", self._model)

        self._language = QComboBox()
        self._language.setEditable(True)
        for code in _LANGS:
            self._language.addItem(code or "(auto)", code)
        wf.addRow("Language:", self._language)

        self._initial_prompt = QLineEdit()
        self._initial_prompt.setPlaceholderText("(none)")
        self._initial_prompt.setToolTip("Optional bias text. Empty is usually fine on medium+.")
        wf.addRow("Initial prompt:", self._initial_prompt)

        self._n_threads = QSpinBox()
        self._n_threads.setRange(1, 32)
        wf.addRow("CPU threads:", self._n_threads)
        outer.addWidget(whisper)

        outer.addStretch(1)
        return page

    def _build_actions_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)

        inject = QGroupBox("Output")
        f = QFormLayout(inject)
        self._paste_mode = QComboBox()
        self._paste_mode.addItems(_PASTE_MODES)
        self._paste_mode.setToolTip(
            "auto = paste only for non-ASCII (preserves clipboard for English).\n"
            "always = paste everything.\n"
            "never = always type via ydotool (fast but loses Polish diacritics)."
        )
        f.addRow("Paste mode:", self._paste_mode)

        self._trailing_space = QCheckBox("Append space between consecutive commits")
        f.addRow("", self._trailing_space)

        self._action_parser = QComboBox()
        self._action_parser.addItems(_PARSERS)
        self._action_parser.setToolTip(
            "regex = fast, deterministic, hand-coded triggers.\n"
            "llm = slower (~300-500ms) but understands natural phrasings."
        )
        f.addRow("Action parser:", self._action_parser)

        self._llm_model = QLineEdit()
        self._llm_model.setPlaceholderText("e.g. gemma4:e4b")
        self._llm_model.setToolTip("Ollama tag of the model used when action_parser = llm.")
        f.addRow("LLM model (ollama tag):", self._llm_model)

        outer.addWidget(inject)
        outer.addStretch(1)
        return page

    def _build_spotify_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)

        info = QLabel(
            "Spotify Web API credentials. Without these, voice search falls back "
            "to opening search results (you click the first one). Get free "
            "credentials at https://developer.spotify.com/dashboard — Client "
            "Credentials flow, no Premium required for search."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: palette(mid);")
        outer.addWidget(info)

        spotify = QGroupBox("Credentials")
        f = QFormLayout(spotify)
        self._spotify_client_id = QLineEdit()
        self._spotify_client_id.setPlaceholderText("(empty = fallback to search-and-click)")
        f.addRow("Client ID:", self._spotify_client_id)

        self._spotify_client_secret = QLineEdit()
        self._spotify_client_secret.setEchoMode(QLineEdit.EchoMode.Password)
        f.addRow("Client Secret:", self._spotify_client_secret)
        outer.addWidget(spotify)
        outer.addStretch(1)
        return page

    def _build_general_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)

        ui = QGroupBox("UI")
        f = QFormLayout(ui)
        self._show_history_on_start = QCheckBox("Show transcript history window on start")
        f.addRow("", self._show_history_on_start)

        self._auto_stop_after_action = QCheckBox(
            "Stop recording automatically after a command executes"
        )
        self._auto_stop_after_action.setToolTip(
            "When on, recording closes after media/Spotify/key-press commands. "
            "Pure dictation and write-mode toggles (Pisz/Przestań) keep the "
            "session open so the typing flow isn't interrupted."
        )
        f.addRow("", self._auto_stop_after_action)
        outer.addWidget(ui)
        outer.addStretch(1)
        return page

    # ---- Populate / save --------------------------------------------------

    def _populate_audio_devices(self) -> None:
        try:
            import sounddevice as sd
            for idx, dev in enumerate(sd.query_devices()):
                if dev.get("max_input_channels", 0) > 0:
                    name = dev.get("name", f"device-{idx}")
                    self._device.addItem(name, name)
        except Exception:
            # Sounddevice unavailable / PortAudio failure — just leave the
            # default option selectable.
            pass

    def _populate_from_config(self) -> None:
        c = self._cfg
        # Audio
        self._gain.setValue(float(c["audio"]["gain"]))
        sr = c["audio"]["sample_rate"]
        idx = self._sample_rate.findText(sr)
        self._sample_rate.setCurrentIndex(idx if idx >= 0 else 0)
        device_name = c["audio"]["device"]
        di = self._device.findData(device_name)
        if di < 0:
            di = self._device.findText(device_name)
        self._device.setCurrentIndex(di if di >= 0 else 0)

        # Whisper
        self._model.setCurrentText(c["whisper"]["model"])
        lang = c["whisper"]["language"]
        li = self._language.findData(lang)
        if li >= 0:
            self._language.setCurrentIndex(li)
        else:
            self._language.setCurrentText(lang)
        self._initial_prompt.setText(c["whisper"]["initial_prompt"])
        self._n_threads.setValue(int(c["whisper"]["n_threads"]))

        # Inject
        self._paste_mode.setCurrentText(c["inject"]["paste_mode"])
        self._trailing_space.setChecked(_truthy(c["inject"]["trailing_space"]))
        self._action_parser.setCurrentText(c["inject"]["action_parser"])
        self._llm_model.setText(c["inject"]["llm_model"])

        # Spotify
        self._spotify_client_id.setText(c["spotify"]["client_id"])
        self._spotify_client_secret.setText(c["spotify"]["client_secret"])

        # UI
        self._show_history_on_start.setChecked(_truthy(c["ui"]["show_history_on_start"]))
        self._auto_stop_after_action.setChecked(_truthy(c["ui"]["auto_stop_after_action"]))

    def _write_to_config(self) -> None:
        c = self._cfg
        c["audio"]["gain"] = f"{self._gain.value():.2f}"
        c["audio"]["sample_rate"] = self._sample_rate.currentText()
        c["audio"]["device"] = self._device.currentData() or ""

        c["whisper"]["model"] = self._model.currentText().strip()
        c["whisper"]["language"] = (self._language.currentData() or self._language.currentText()).strip()
        c["whisper"]["initial_prompt"] = self._initial_prompt.text()
        c["whisper"]["n_threads"] = str(self._n_threads.value())

        c["inject"]["paste_mode"] = self._paste_mode.currentText()
        c["inject"]["trailing_space"] = "true" if self._trailing_space.isChecked() else "false"
        c["inject"]["action_parser"] = self._action_parser.currentText()
        c["inject"]["llm_model"] = self._llm_model.text().strip()

        c["spotify"]["client_id"] = self._spotify_client_id.text().strip()
        c["spotify"]["client_secret"] = self._spotify_client_secret.text().strip()

        c["ui"]["show_history_on_start"] = "true" if self._show_history_on_start.isChecked() else "false"
        c["ui"]["auto_stop_after_action"] = "true" if self._auto_stop_after_action.isChecked() else "false"

    def _on_apply(self) -> None:
        self._save_and_notify()

    def _on_ok(self) -> None:
        self._save_and_notify()
        self.accept()

    def _save_and_notify(self) -> None:
        try:
            self._write_to_config()
            config.save(self._cfg)
            self.settings_saved.emit()
        except Exception as e:
            QMessageBox.critical(self, "Failed to save", str(e))


def _truthy(s: str) -> bool:
    return s.strip().lower() in {"1", "true", "yes", "on"}
