"""Settings dialog — exposes every key in ~/.config/korderrc as form fields,
organized in tabs. Saves write back to disk; user is told to restart for the
changes to fully take effect."""
from __future__ import annotations
from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStyle,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from korder import config
from korder.ui.benchmark import BenchmarkDialog

_WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]
_LANGS = ["", "pl", "en", "de", "fr", "es", "it", "uk", "cs", "ru"]
_PASTE_MODES = ["auto", "always", "never"]
_PARSERS = ["regex", "llm"]
_SAMPLE_RATES = ["16000", "32000", "48000"]

# KDE HIG spacings (px). Dialog outer margins and inter-widget spacing are
# 9 / 6 respectively in Plasma's own configuration modules.
_MARGIN = 9
_SPACING = 6


class _InfoBanner(QFrame):
    """KMessageWidget-style information banner.

    Tints the panel background by mixing Window and Highlight from the active
    palette, so it stays legible on both light and dark themes and re-renders
    when the user changes themes mid-session.
    """

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("KorderInfoBanner")
        self.setAutoFillBackground(True)
        self.setFrameShape(QFrame.Shape.NoFrame)

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(8)

        icon_label = QLabel(self)
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation)
        icon_label.setPixmap(icon.pixmap(16, 16))
        icon_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        row.addWidget(icon_label)

        self._text = QLabel(text, self)
        self._text.setWordWrap(True)
        self._text.setOpenExternalLinks(True)
        self._text.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        row.addWidget(self._text, 1)

        self._apply_tint()

    def _apply_tint(self) -> None:
        pal = self.palette()
        window = pal.color(QPalette.ColorRole.Window)
        highlight = pal.color(QPalette.ColorRole.Highlight)
        ratio = 0.18
        tinted = QColor(
            int(ratio * highlight.red() + (1 - ratio) * window.red()),
            int(ratio * highlight.green() + (1 - ratio) * window.green()),
            int(ratio * highlight.blue() + (1 - ratio) * window.blue()),
        )
        pal.setColor(QPalette.ColorRole.Window, tinted)
        self.setPalette(pal)
        self.setStyleSheet("QFrame#KorderInfoBanner { border-radius: 4px; }")

    def changeEvent(self, event: QEvent) -> None:
        if event.type() in (
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.StyleChange,
        ):
            self._apply_tint()
        super().changeEvent(event)


def _hint_label(text: str) -> QLabel:
    """De-emphasized helper text using the theme's PlaceholderText role.

    Set via QPalette (not stylesheet) because Qt stylesheet's ``palette(...)``
    function does not expose ``placeholder-text``.
    """
    label = QLabel(text)
    label.setWordWrap(True)
    pal = label.palette()
    pal.setColor(
        QPalette.ColorRole.WindowText,
        pal.color(QPalette.ColorRole.PlaceholderText),
    )
    label.setPalette(pal)
    return label


def _make_form(parent: QWidget) -> QFormLayout:
    """Form layout with KDE HIG-aligned policies."""
    f = QFormLayout(parent)
    f.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    f.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    f.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
    f.setHorizontalSpacing(_SPACING * 2)
    f.setVerticalSpacing(_SPACING)
    return f


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
        layout.setContentsMargins(_MARGIN, _MARGIN, _MARGIN, _MARGIN)
        layout.setSpacing(_SPACING)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_audio_whisper_tab(), "Mic && Whisper")
        self._tabs.addTab(self._build_actions_tab(), "Actions && Output")
        self._tabs.addTab(self._build_spotify_tab(), "Spotify")
        self._tabs.addTab(self._build_general_tab(), "General")
        layout.addWidget(self._tabs, 1)

        hint = _hint_label("Most settings take effect after restarting Korder.")
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
        outer.setContentsMargins(_MARGIN, _MARGIN, _MARGIN, _MARGIN)
        outer.setSpacing(_SPACING * 2)

        # Audio group
        audio = QGroupBox("Microphone")
        af = _make_form(audio)
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

        self._duck_during_recording = QCheckBox(
            "Lower system volume while listening"
        )
        self._duck_during_recording.setToolTip(
            "Ducks the default PipeWire sink while recording so speaker "
            "bleed doesn't confuse Whisper. Original level is restored "
            "on stop (and on crash via atexit). Requires wpctl."
        )
        af.addRow("", self._duck_during_recording)

        self._duck_volume_pct = QSpinBox()
        self._duck_volume_pct.setRange(0, 100)
        self._duck_volume_pct.setSuffix(" %")
        self._duck_volume_pct.setToolTip(
            "Target volume while listening, as a percentage of full. "
            "30 % is usually enough to prevent bleed without going silent."
        )
        af.addRow("Duck to:", self._duck_volume_pct)
        outer.addWidget(audio)

        # Whisper group
        whisper = QGroupBox("Whisper")
        wf = _make_form(whisper)
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
        outer.setContentsMargins(_MARGIN, _MARGIN, _MARGIN, _MARGIN)
        outer.setSpacing(_SPACING * 2)

        inject = QGroupBox("Output")
        f = _make_form(inject)
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

        self._intent_thinking_mode = QCheckBox(
            "Use Gemma's thinking mode (slower but reasons better)"
        )
        self._intent_thinking_mode.setToolTip(
            "Enables Gemma's 'think before answering' step. Adds ~1-2s of "
            "latency per command but resolves ambiguous phrasings without "
            "hand-coded triggers. Only affects LLM action parser."
        )
        f.addRow("", self._intent_thinking_mode)

        self._intent_show_triggers = QCheckBox(
            "Show full trigger lists in LLM prompt (legacy / explicit matching)"
        )
        self._intent_show_triggers.setToolTip(
            "When off (default), the LLM only sees action descriptions and "
            "must reason about whether an utterance matches. When on, every "
            "trigger phrase per action is included in the prompt — more "
            "deterministic but bigger prompt and worse generalization."
        )
        f.addRow("", self._intent_show_triggers)

        self._intent_timeout = QSpinBox()
        self._intent_timeout.setRange(2, 120)
        self._intent_timeout.setSuffix(" s")
        self._intent_timeout.setToolTip(
            "Per-call timeout for the LLM action parser. Thinking mode "
            "regularly takes 3-5s and slow cases can hit 6+; the default "
            "of 20s means a transient slow call won't trip the fallback "
            "to regex (which loses parameterized actions like Spotify "
            "search)."
        )
        f.addRow("LLM timeout:", self._intent_timeout)

        # Subtle separator + right-aligned benchmark action. Spanning the
        # form's full width (single-arg addRow) avoids the field-column
        # indent the empty-label form rows produce.
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        f.addRow(sep)

        bench_row = QHBoxLayout()
        bench_row.setContentsMargins(0, 0, 0, 0)
        bench_row.setSpacing(_SPACING)
        bench_hint = _hint_label(
            "Measure latency and accuracy with the LLM settings above."
        )
        bench_row.addWidget(bench_hint, 1)
        self._bench_btn = QPushButton("Run benchmark…")
        self._bench_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        )
        self._bench_btn.setToolTip(
            "Run a fixed suite of utterances against the currently configured "
            "LLM model and toggles, then report per-case correctness and "
            "latency. Uses the values currently shown in this dialog (no "
            "save required)."
        )
        self._bench_btn.clicked.connect(self._on_benchmark)
        bench_row.addWidget(self._bench_btn)
        f.addRow(bench_row)

        outer.addWidget(inject)
        outer.addStretch(1)
        return page

    def _build_spotify_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(_MARGIN, _MARGIN, _MARGIN, _MARGIN)
        outer.setSpacing(_SPACING * 2)

        info = _InfoBanner(
            "Spotify Web API credentials. Without these, voice search falls back "
            "to opening search results (you click the first one). Get free "
            'credentials at <a href="https://developer.spotify.com/dashboard">'
            "developer.spotify.com/dashboard</a> — Client Credentials flow, "
            "no Premium required for search."
        )
        outer.addWidget(info)

        spotify = QGroupBox("Credentials")
        f = _make_form(spotify)
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
        outer.setContentsMargins(_MARGIN, _MARGIN, _MARGIN, _MARGIN)
        outer.setSpacing(_SPACING * 2)

        ui = QGroupBox("UI")
        f = _make_form(ui)
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
        self._duck_during_recording.setChecked(
            _truthy(c["audio"].get("duck_during_recording", "false"))
        )
        try:
            self._duck_volume_pct.setValue(int(c["audio"].get("duck_volume_pct", "30")))
        except (KeyError, ValueError):
            self._duck_volume_pct.setValue(30)

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

        # Intent (LLM tuning)
        self._intent_thinking_mode.setChecked(_truthy(c["intent"]["thinking_mode"]))
        self._intent_show_triggers.setChecked(_truthy(c["intent"]["show_triggers_in_prompt"]))
        try:
            self._intent_timeout.setValue(int(float(c["intent"]["timeout_s"])))
        except (KeyError, ValueError):
            self._intent_timeout.setValue(20)

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
        c["audio"]["duck_during_recording"] = (
            "true" if self._duck_during_recording.isChecked() else "false"
        )
        c["audio"]["duck_volume_pct"] = str(self._duck_volume_pct.value())

        c["whisper"]["model"] = self._model.currentText().strip()
        c["whisper"]["language"] = (self._language.currentData() or self._language.currentText()).strip()
        c["whisper"]["initial_prompt"] = self._initial_prompt.text()
        c["whisper"]["n_threads"] = str(self._n_threads.value())

        c["inject"]["paste_mode"] = self._paste_mode.currentText()
        c["inject"]["trailing_space"] = "true" if self._trailing_space.isChecked() else "false"
        c["inject"]["action_parser"] = self._action_parser.currentText()
        c["inject"]["llm_model"] = self._llm_model.text().strip()

        c["intent"]["thinking_mode"] = (
            "true" if self._intent_thinking_mode.isChecked() else "false"
        )
        c["intent"]["show_triggers_in_prompt"] = (
            "true" if self._intent_show_triggers.isChecked() else "false"
        )
        c["intent"]["timeout_s"] = str(self._intent_timeout.value())

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

    def _on_benchmark(self) -> None:
        """Run the intent benchmark with the values currently shown in the
        dialog — not whatever's saved on disk. Lets the user iterate on
        toggles without having to Apply + restart between trials."""
        model = self._llm_model.text().strip() or "gemma4:e4b"
        thinking = self._intent_thinking_mode.isChecked()
        triggers = self._intent_show_triggers.isChecked()
        timeout_s = float(self._intent_timeout.value())
        dialog = BenchmarkDialog(model, thinking, triggers, timeout_s, self)
        dialog.exec()


def _truthy(s: str) -> bool:
    return s.strip().lower() in {"1", "true", "yes", "on"}
