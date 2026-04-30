"""Smoke test for the settings dialog — verifies it constructs from the
current config without exceptions and round-trips field values back into
the config object on save. Doesn't actually show the dialog (no Qt main
loop), so the test runs headless."""
from __future__ import annotations
import os
import sys
import pytest


# QApplication needs to exist before any QWidget; create one shared instance.
@pytest.fixture(scope="module")
def qapp():
    # Use offscreen platform so this works in CI without a display
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_dialog_constructs_with_default_config(qapp, monkeypatch, tmp_path):
    """Dialog should construct and populate fields without raising even when
    no user config file exists (only DEFAULTS apply)."""
    from korder import config
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "korderrc")

    from korder.ui.settings_dialog import SettingsDialog
    dlg = SettingsDialog()
    # Spot-check a few fields populated from defaults
    assert dlg._gain.value() == pytest.approx(0.7)
    assert dlg._model.currentText() == "medium"
    assert dlg._action_parser.currentText() == "regex"
    assert dlg._spotify_client_id.text() == ""
    dlg.deleteLater()


def test_save_writes_changed_values_back_to_config(qapp, monkeypatch, tmp_path):
    """Mutate a few fields, call _save_and_notify, verify the new config
    file has the new values."""
    from korder import config
    fake_path = tmp_path / "korderrc"
    monkeypatch.setattr(config, "CONFIG_PATH", fake_path)

    from korder.ui.settings_dialog import SettingsDialog
    dlg = SettingsDialog()

    # Change gain from 0.7 → 0.85
    dlg._gain.setValue(0.85)
    # Switch action parser
    dlg._action_parser.setCurrentText("llm")
    # Set Spotify creds
    dlg._spotify_client_id.setText("the-client-id")
    dlg._spotify_client_secret.setText("the-secret")

    dlg._save_and_notify()

    # Reload config from disk and verify
    cfg = config.load()
    assert cfg["audio"]["gain"] == "0.85"
    assert cfg["inject"]["action_parser"] == "llm"
    assert cfg["spotify"]["client_id"] == "the-client-id"
    assert cfg["spotify"]["client_secret"] == "the-secret"
    dlg.deleteLater()
