"""Pytest config — adds an `ollama` marker for integration tests that
hit a live ollama instance. Auto-skips when the service is unreachable
or the required model isn't pulled."""
from __future__ import annotations
import json
import os
import urllib.error
import urllib.request

import pytest


_OLLAMA_HEALTH_URL = "http://localhost:11434/api/tags"
_DEFAULT_INTEGRATION_MODEL = os.environ.get("KORDER_INTEGRATION_MODEL", "gemma4:e4b")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "ollama: integration tests that hit a real ollama HTTP endpoint. "
        "Auto-skipped if the service is unreachable or the configured model "
        "isn't pulled. Set KORDER_INTEGRATION_MODEL env to use a different tag.",
    )


def _is_ollama_reachable_with_model(model: str) -> tuple[bool, str]:
    """Returns (available, reason). Reason is empty on success."""
    try:
        req = urllib.request.Request(_OLLAMA_HEALTH_URL)
        with urllib.request.urlopen(req, timeout=2) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        return False, f"ollama unreachable at localhost:11434 ({e})"
    tags = {m.get("name") for m in body.get("models", []) if isinstance(m, dict)}
    # Tag matching: exact, or with :latest suffix elided
    candidates = {model, f"{model}:latest"}
    if not (candidates & tags):
        return False, f"model {model!r} not pulled (have: {sorted(tags)})"
    return True, ""


@pytest.fixture(scope="session")
def integration_model() -> str:
    available, reason = _is_ollama_reachable_with_model(_DEFAULT_INTEGRATION_MODEL)
    if not available:
        pytest.skip(reason)
    return _DEFAULT_INTEGRATION_MODEL
