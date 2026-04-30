"""Intent-parser benchmark UI: thin Qt wrapper around the headless suite
in korder.intent_bench. Used from the Settings dialog so tuning the LLM
toggles (model, thinking mode, trigger visibility) can be validated
immediately without restarting Korder.

The work runs on a QThread so the dialog stays responsive while ollama
is generating. One untimed warmup call is fired first (inside run_suite)
to absorb any initial load latency.
"""
from __future__ import annotations
import statistics
from typing import Optional

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QLabel,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from korder.actions.base import get_action
from korder.intent import IntentParser
from korder.intent_bench import CASES as _CASES, BenchResult, run_suite


class _BenchmarkWorker(QThread):
    """Runs the benchmark suite on a background thread.
    Emits `progress(idx, total)` per case and `finished_results(list)`
    once done. `failed(message)` if the warmup call itself errors out
    (typically: ollama unreachable)."""

    progress = Signal(int, int)
    finished_results = Signal(list)
    failed = Signal(str)

    def __init__(
        self,
        model: str,
        thinking_mode: bool,
        show_triggers: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._parser = IntentParser(
            model=model,
            timeout_s=30.0,  # thinking mode can blow past the default
            thinking_mode=thinking_mode,
            show_triggers_in_prompt=show_triggers,
        )

    def run(self) -> None:
        try:
            results = run_suite(
                self._parser,
                progress=lambda i, t: self.progress.emit(i, t),
            )
        except Exception as e:
            self.failed.emit(f"Benchmark failed: {e}")
            return
        self.finished_results.emit(results)


class BenchmarkDialog(QDialog):
    """Modal dialog that runs the benchmark and displays the results.
    Reads its parameters in the constructor (so the *current* UI values
    in the parent settings dialog are used, not whatever's on disk)."""

    def __init__(
        self,
        model: str,
        thinking_mode: bool,
        show_triggers: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Intent benchmark")
        self.resize(720, 480)
        self._build_ui(model, thinking_mode, show_triggers)

        self._worker = _BenchmarkWorker(model, thinking_mode, show_triggers, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_results.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _build_ui(self, model: str, thinking_mode: bool, show_triggers: bool) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 9, 9, 9)
        layout.setSpacing(9)

        # Configuration summary as a small grouped form so the labels align
        # cleanly under one heading instead of running together on one line.
        config_group = QGroupBox("Configuration")
        cf = QFormLayout(config_group)
        cf.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        cf.setHorizontalSpacing(12)
        cf.setVerticalSpacing(4)
        cf.setContentsMargins(9, 9, 9, 9)
        cf.addRow("Model:", QLabel(model))
        cf.addRow("Thinking:", QLabel("on" if thinking_mode else "off"))
        cf.addRow("Triggers in prompt:", QLabel("on" if show_triggers else "off"))
        layout.addWidget(config_group)

        # Progress block: status text + bar stacked, status sits left-aligned
        # so it doesn't fight the bar for visual weight.
        self._status = QLabel("Warming up model…")
        layout.addWidget(self._status)

        self._progress = QProgressBar()
        self._progress.setRange(0, len(_CASES))
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        layout.addWidget(self._progress)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["Utterance", "Expected", "Got", "Latency", "OK"]
        )
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        for col in (1, 2, 3, 4):
            self._table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents
            )
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        layout.addWidget(self._table, 1)

        # Summary line with theme-tinted weight so it reads as the bottom-line
        # answer (correctness + perf) without screaming.
        self._summary = QLabel("")
        self._summary.setTextFormat(Qt.TextFormat.RichText)
        self._summary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sf = self._summary.font()
        sf.setPointSizeF(sf.pointSizeF() + 0.5)
        self._summary.setFont(sf)
        layout.addWidget(self._summary)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        # Close button reports "rejected" by default — wire both so any path closes.
        close_btn = btns.button(QDialogButtonBox.StandardButton.Close)
        close_btn.clicked.connect(self.accept)
        layout.addWidget(btns)

    def _on_progress(self, idx: int, total: int) -> None:
        self._progress.setMaximum(total)
        self._progress.setValue(idx)
        self._status.setText(f"Running {idx}/{total}…")

    def _on_failed(self, message: str) -> None:
        self._status.setText(message)
        self._progress.setRange(0, 1)
        self._progress.setValue(0)

    def _on_done(self, results: list) -> None:
        self._status.setText("Done.")
        self._progress.setValue(self._progress.maximum())
        self._populate_table(results)
        self._populate_summary(results)

    def _populate_table(self, results: list[BenchResult]) -> None:
        # KDE colour palette uses theme-friendly green/red ("positive" /
        # "negative" roles in Plasma). Hardcoding mid-saturation values
        # gives reasonable contrast on both light and dark themes; the
        # text stays bold so it's readable even when the row is selected.
        green = QColor(46, 174, 102)
        red = QColor(218, 68, 83)
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)

        self._table.setRowCount(len(results))
        for row, r in enumerate(results):
            self._set_cell(row, 0, r.utterance, tooltip=r.note)
            self._set_cell(row, 1, _fmt_expected(r.expected_action))
            got_text = r.got_action if r.got_action is not None else "(none)"
            if r.error:
                got_text = f"ERROR: {r.error}"
            # When thinking mode was on, surface Gemma's reasoning trace as
            # a tooltip on the Got cell — hover to see why the LLM made
            # this call. Truncate to keep the tooltip pop-up sane.
            got_tooltip = ""
            if r.thinking:
                trimmed = r.thinking
                if len(trimmed) > 1200:
                    trimmed = trimmed[:1200] + "…"
                got_tooltip = f"Gemma's reasoning:\n\n{trimmed}"
            self._set_cell(row, 2, got_text, tooltip=got_tooltip)
            self._set_cell(
                row,
                3,
                f"{r.latency_ms:.0f} ms",
                font=mono,
                align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            )
            ok_tooltip = ""
            if not r.ok and r.got_action == r.expected_action and not r.pipeline_ok:
                ok_tooltip = (
                    "LLM picked the right action name, but its returned "
                    "phrase wasn't a verbatim substring of the input — "
                    "the pipeline would fall back to regex."
                )
            ok_font = QFont()
            ok_font.setBold(True)
            self._set_cell(
                row,
                4,
                "✓" if r.ok else "✗",
                fg=green if r.ok else red,
                tooltip=ok_tooltip,
                font=ok_font,
                align=Qt.AlignmentFlag.AlignCenter,
            )

    def _set_cell(
        self,
        row: int,
        col: int,
        text: str,
        tooltip: str = "",
        fg: QColor | None = None,
        font: QFont | None = None,
        align: Qt.AlignmentFlag | None = None,
    ) -> None:
        item = QTableWidgetItem(text)
        if tooltip:
            item.setToolTip(tooltip)
        if fg is not None:
            item.setForeground(fg)
        if font is not None:
            item.setFont(font)
        if align is not None:
            item.setTextAlignment(align)
        self._table.setItem(row, col, item)

    def _populate_summary(self, results: list[BenchResult]) -> None:
        if not results:
            self._summary.setText("")
            return
        latencies = [r.latency_ms for r in results if r.error is None]
        passes = sum(1 for r in results if r.ok)
        total = len(results)
        avg = statistics.fmean(latencies) if latencies else 0.0
        med = statistics.median(latencies) if latencies else 0.0
        lo = min(latencies) if latencies else 0.0
        hi = max(latencies) if latencies else 0.0
        # Color the pass count using the same green/red as the OK column
        # so the bottom-line answer is readable at a glance.
        color = "#2eae66" if passes == total else "#da4453"
        self._summary.setText(
            f"<span style='color:{color};'><b>{passes} / {total}</b> correct</span> "
            f"&nbsp;·&nbsp; "
            f"avg <b>{avg:.0f} ms</b> · "
            f"median {med:.0f} ms · "
            f"min {lo:.0f} · max {hi:.0f}"
        )


def _fmt_expected(name: Optional[str]) -> str:
    if name is None:
        return "(no action)"
    action = get_action(name)
    if action is None:
        return name
    return name
