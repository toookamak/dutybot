from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DUTYCTL = ROOT / "helper" / "dutyctl"


def run_ctl(args, env) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(DUTYCTL), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def base_env(tmp_state: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["DUTYBOT_WATCH_JSON"] = str(tmp_state / "watch.json")
    env["DUTYBOT_PREVIEW_FILE"] = str(tmp_state / "kill-preview.json")
    env["DUTYCTL_DRY_RUN"] = "1"
    return env


def test_rejects_unknown_command(tmp_state: Path):
    env = base_env(tmp_state)
    cp = run_ctl(["shell"], env)
    assert cp.returncode != 0


def test_rejects_unknown_unit(tmp_state: Path):
    watch = tmp_state / "watch.json"
    watch.write_text(
        json.dumps({"services": [{"id": "a", "name": "A", "unit": "a.service"}]}),
        encoding="utf-8",
    )
    env = base_env(tmp_state)
    cp = run_ctl(["restart-unit", "evil.service"], env)
    assert cp.returncode != 0
    cp2 = run_ctl(["restart-unit", "not a unit"], env)
    assert cp2.returncode != 0


def test_restart_unit_listed_exit_0(tmp_state: Path):
    watch = tmp_state / "watch.json"
    watch.write_text(
        json.dumps({"services": [{"id": "a", "name": "A", "unit": "foo.service"}]}),
        encoding="utf-8",
    )
    env = base_env(tmp_state)
    cp = run_ctl(["restart-unit", "foo.service"], env)
    assert cp.returncode == 0
    assert "OK" in (cp.stdout or "")


def test_rejects_pid_not_in_preview(tmp_state: Path):
    preview = tmp_state / "kill-preview.json"
    preview.write_text(
        json.dumps(
            {
                "token": "previewtoken01",
                "pids": [4242],
                "expires": time.time() + 600,
            }
        ),
        encoding="utf-8",
    )
    env = base_env(tmp_state)
    cp = run_ctl(["kill-pids", "previewtoken01", "99999"], env)
    assert cp.returncode != 0
    cp2 = run_ctl(["kill-pids", "wrongtoken01", "4242"], env)
    assert cp2.returncode != 0
