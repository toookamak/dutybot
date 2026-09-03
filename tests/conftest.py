from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DUTYBOT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DUTYBOT_WATCH_JSON", str(tmp_path / "watch.json"))
    monkeypatch.setenv("DUTYBOT_PREVIEW_FILE", str(tmp_path / "kill-preview.json"))
    monkeypatch.setenv("DUTYBOT_ENV", str(tmp_path / "env"))
    return tmp_path
