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
# openwakeword 0.6 stock catalog — common wake phrases that ship as
# pretrained models (downloaded on first use). Custom phrases require
# training your own model; the editable combo in the Settings dialog
# still accepts arbitrary text for that case.
_WAKE_PHRASES = ["hey_jarvis", "alexa", "hey_mycroft", "hey_rhasspy"]
_WAKE_ENGINES = ["openwakeword"]

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
        self._tabs.addTab(self._build_wake_tab(), "Wake word")
        self._tabs.addTab(self._build_speech_tab(), "Speech")
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

        self._start_chime = QCheckBox("Play soft chime when dictation starts")
        self._start_chime.setToolTip(
            "Plays a 200 ms two-tone cue when the mic opens (hotkey "
            "or wake-word). Mic capture is deliberately deferred until "
            "the chime finishes so Whisper doesn't transcribe it via "
            "speaker bleed. Adds ~250 ms perceived latency in exchange "
            "for an audible 'go' signal."
        )
        af.addRow("", self._start_chime)
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
            "Use Gemma's thinking mode (off by default — slower, no measured accuracy win)"
        )
        self._intent_thinking_mode.setToolTip(
            "Enables Gemma's 'think before answering' step. Adds ~1.3s of "
            "latency per command on E4B (and ~3.4s on E2B). On the current "
            "intent benchmark it does not improve pass rate, so it's off by "
            "default; turn on only if you've identified a phrasing the "
            "default path mishandles. Only affects LLM action parser."
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

        self._intent_keep_alive = QSpinBox()
        self._intent_keep_alive.setRange(0, 3600)
        self._intent_keep_alive.setSuffix(" s")
        self._intent_keep_alive.setSpecialValueText("0 — unload immediately")
        self._intent_keep_alive.setToolTip(
            "How long ollama keeps the model resident in VRAM after a "
            "call. Default 300s keeps follow-up calls warm (~0.2s); 0 "
            "unloads immediately and frees ~9.5 GB on E4B at the cost "
            "of a ~3s cold-load on the next call. Lower this if you'd "
            "rather have VRAM for other apps between dictation bursts."
        )
        f.addRow("LLM keep-alive:", self._intent_keep_alive)

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

    def _build_wake_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(_MARGIN, _MARGIN, _MARGIN, _MARGIN)
        outer.setSpacing(_SPACING * 2)

        info = _InfoBanner(
            "Hands-free activation: speak the wake phrase to start a "
            "dictation session as if you'd pressed the hotkey. The "
            "hotkey path keeps working alongside. Requires the "
            "<code>wake</code> optional extra: install with "
            "<code>uv sync --extra wake</code>."
        )
        outer.addWidget(info)

        wake = QGroupBox("Wake word")
        f = _make_form(wake)

        self._wake_enabled = QCheckBox("Listen for the wake phrase while idle")
        self._wake_enabled.setToolTip(
            "When checked, the mic stays open and the configured phrase "
            "triggers a dictation session. Off by default — manual "
            "hotkey is the only activation path."
        )
        f.addRow("", self._wake_enabled)

        self._wake_engine = QComboBox()
        self._wake_engine.addItems(_WAKE_ENGINES)
        self._wake_engine.setToolTip("Detection backend. Only openwakeword today.")
        f.addRow("Engine:", self._wake_engine)

        self._wake_phrase = QComboBox()
        self._wake_phrase.setEditable(True)
        self._wake_phrase.addItems(_WAKE_PHRASES)
        self._wake_phrase.setToolTip(
            "openwakeword catalog model name. The four stock options are "
            "pretrained and download on first use; custom phrases require "
            "training your own ONNX model and pointing at it by name."
        )
        f.addRow("Phrase:", self._wake_phrase)

        self._wake_sensitivity = QDoubleSpinBox()
        self._wake_sensitivity.setRange(0.10, 0.95)
        self._wake_sensitivity.setSingleStep(0.05)
        self._wake_sensitivity.setDecimals(2)
        self._wake_sensitivity.setToolTip(
            "Detection threshold. Lower fires more often (more false "
            "positives), higher misses softer wakes. 0.5 is a reasonable "
            "starting point; tune up if your phrase fires randomly, down "
            "if it misses calls."
        )
        f.addRow("Sensitivity:", self._wake_sensitivity)

        self._wake_idle_timeout = QSpinBox()
        self._wake_idle_timeout.setRange(0, 30)
        self._wake_idle_timeout.setSuffix(" s")
        self._wake_idle_timeout.setSpecialValueText("0 — never auto-cancel")
        self._wake_idle_timeout.setToolTip(
            "If the wake phrase fires but no follow-up speech arrives "
            "within this many seconds, the dictation cancels back to "
            "wake-listening. Catches accidental wakes that would "
            "otherwise leave the OSD stuck open. 0 disables the timeout."
        )
        f.addRow("Idle timeout:", self._wake_idle_timeout)

        outer.addWidget(wake)
        outer.addStretch(1)
        return page

    def _build_speech_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(_MARGIN, _MARGIN, _MARGIN, _MARGIN)
        outer.setSpacing(_SPACING * 2)

        info = _InfoBanner(
            "Spoken responses: certain actions (like \"what's playing\") "
            "can read their result aloud. Uses Piper, a local neural TTS "
            "engine — voices auto-download to <code>~/.local/share/piper</code> "
            "on first use. Requires the <code>tts</code> optional extra: "
            "install with <code>uv sync --extra tts</code>."
        )
        outer.addWidget(info)

        speech = QGroupBox("Speech")
        f = _make_form(speech)

        self._tts_enabled = QCheckBox("Speak action responses aloud")
        self._tts_enabled.setToolTip(
            "When checked, actions that produce a spoken response "
            "(now_playing today; future weather/time/date) read their "
            "result through TTS. Off by default."
        )
        f.addRow("", self._tts_enabled)

        self._tts_voice_en = QLineEdit()
        self._tts_voice_en.setPlaceholderText("en_US-amy-medium")
        self._tts_voice_en.setToolTip(
            "Piper voice ID for English. Browse available voices at "
            "https://huggingface.co/rhasspy/piper-voices. Pre-fetch with "
            "`python -m piper.download_voices VOICE_ID`."
        )
        f.addRow("English voice:", self._tts_voice_en)

        self._tts_voice_pl = QLineEdit()
        self._tts_voice_pl.setPlaceholderText("pl_PL-darkman-medium")
        self._tts_voice_pl.setToolTip(
            "Piper voice ID for Polish. Same catalogue as English; "
            "pl_PL voices are scarcer than en_US — pl_PL-darkman-medium "
            "and pl_PL-gosia-medium are the common picks."
        )
        f.addRow("Polish voice:", self._tts_voice_pl)

        self._tts_speed = QDoubleSpinBox()
        self._tts_speed.setRange(0.5, 2.0)
        self._tts_speed.setSingleStep(0.1)
        self._tts_speed.setDecimals(2)
        self._tts_speed.setToolTip(
            "Playback speed multiplier. 1.0 is the voice's natural rate; "
            "increase to ~1.3 for faster delivery, decrease for slower. "
            "Piper applies this via length-scale internally."
        )
        f.addRow("Speed:", self._tts_speed)

        self._tts_suppress_when_playing = QCheckBox(
            "Stay silent when something is already playing"
        )
        self._tts_suppress_when_playing.setToolTip(
            "When checked, TTS doesn't fire if any MPRIS player is "
            "currently Playing. Avoids talking over music. With this "
            "off, TTS pauses the player, speaks, and resumes it — "
            "clean dropout window but more invasive."
        )
        f.addRow("", self._tts_suppress_when_playing)

        self._tts_speak_action_progress = QCheckBox(
            "Also voice action progress narration"
        )
        self._tts_speak_action_progress.setToolTip(
            "When checked, every progress narration emitted via the "
            "speak bus is voiced (not just speakable_response actions). "
            "Off by default — keeps TTS scoped to query answers rather "
            "than chatty 'Searching Spotify…' lines."
        )
        f.addRow("", self._tts_speak_action_progress)

        outer.addWidget(speech)
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
        self._start_chime.setChecked(_truthy(c["audio"].get("start_chime", "true")))

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
        try:
            self._intent_keep_alive.setValue(int(float(c["intent"]["keep_alive_s"])))
        except (KeyError, ValueError):
            self._intent_keep_alive.setValue(300)

        # Spotify
        self._spotify_client_id.setText(c["spotify"]["client_id"])
        self._spotify_client_secret.setText(c["spotify"]["client_secret"])

        # Wake word
        self._wake_enabled.setChecked(_truthy(c["wake"].get("enabled", "false")))
        engine = c["wake"].get("engine", "openwakeword")
        ei = self._wake_engine.findText(engine)
        self._wake_engine.setCurrentIndex(ei if ei >= 0 else 0)
        phrase = c["wake"].get("phrase", "hey_jarvis")
        pi = self._wake_phrase.findText(phrase)
        if pi >= 0:
            self._wake_phrase.setCurrentIndex(pi)
        else:
            self._wake_phrase.setCurrentText(phrase)
        try:
            self._wake_sensitivity.setValue(float(c["wake"].get("sensitivity", "0.5")))
        except (KeyError, ValueError):
            self._wake_sensitivity.setValue(0.5)
        try:
            self._wake_idle_timeout.setValue(int(float(c["wake"].get("idle_timeout_s", "5"))))
        except (KeyError, ValueError):
            self._wake_idle_timeout.setValue(5)

        # TTS / Speech
        self._tts_enabled.setChecked(_truthy(c["tts"].get("enabled", "false")))
        self._tts_voice_en.setText(c["tts"].get("voice_en", "en_US-amy-medium"))
        self._tts_voice_pl.setText(c["tts"].get("voice_pl", "pl_PL-darkman-medium"))
        try:
            self._tts_speed.setValue(float(c["tts"].get("speed", "1.0")))
        except (KeyError, ValueError):
            self._tts_speed.setValue(1.0)
        self._tts_suppress_when_playing.setChecked(
            _truthy(c["tts"].get("suppress_when_playing", "true"))
        )
        self._tts_speak_action_progress.setChecked(
            _truthy(c["tts"].get("speak_action_progress", "false"))
        )

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
        c["audio"]["start_chime"] = "true" if self._start_chime.isChecked() else "false"

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
        c["intent"]["keep_alive_s"] = str(self._intent_keep_alive.value())

        c["spotify"]["client_id"] = self._spotify_client_id.text().strip()
        c["spotify"]["client_secret"] = self._spotify_client_secret.text().strip()

        c["wake"]["enabled"] = "true" if self._wake_enabled.isChecked() else "false"
        c["wake"]["engine"] = self._wake_engine.currentText().strip()
        c["wake"]["phrase"] = self._wake_phrase.currentText().strip() or "hey_jarvis"
        c["wake"]["sensitivity"] = f"{self._wake_sensitivity.value():.2f}"
        c["wake"]["idle_timeout_s"] = str(self._wake_idle_timeout.value())

        c["tts"]["enabled"] = "true" if self._tts_enabled.isChecked() else "false"
        c["tts"]["voice_en"] = self._tts_voice_en.text().strip() or "en_US-amy-medium"
        c["tts"]["voice_pl"] = self._tts_voice_pl.text().strip() or "pl_PL-darkman-medium"
        c["tts"]["speed"] = f"{self._tts_speed.value():.2f}"
        c["tts"]["suppress_when_playing"] = (
            "true" if self._tts_suppress_when_playing.isChecked() else "false"
        )
        c["tts"]["speak_action_progress"] = (
            "true" if self._tts_speak_action_progress.isChecked() else "false"
        )

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
