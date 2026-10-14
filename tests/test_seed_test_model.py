"""agent/testing/seed_test_model.py: a throwaway app-data dir the CLI runs with.

Each run is its own process: ``settings`` binds its data directory on import.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
SEED = [sys.executable, str(ROOT / "agent" / "testing" / "seed_test_model.py")]


def _run(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)


def test_the_engine_runs_with_the_seeded_model(tmp_path: Path):
    data = tmp_path / "app-data"
    env = {**os.environ, "TRANSLATOR_TEST_API_KEY": "sk-test-0123456789abcdef"}
    env.pop("TRANSLATOR_APP_DATA_DIR", None)
    env.pop("TRANSLATOR_SIDECAR_PARENT_PID", None)

    seeded = _run(
        [*SEED, "--target", str(data), "--base-url", "https://api.example.invalid/v1", "--model", "cheap", "--concurrency", "3"],
        env,
    )

    assert seeded.returncode == 0, seeded.stderr
    report = json.loads(seeded.stdout.splitlines()[-1])
    assert (report["model"], report["concurrency"]["word"]) == ("cheap", 3)
    keys = json.loads((data / "keys.json").read_text(encoding="utf-8"))
    assert keys == {"custom_openai::https://api.example.invalid/v1": "sk-test-0123456789abcdef"}

    workbook = Workbook()
    workbook.active["A1"] = "钢筋"
    workbook.save(tmp_path / "a.xlsx")
    checked = _run(
        [sys.executable, "-m", "cli", "translate", str(tmp_path / "a.xlsx"), "--to", "fr", "--dry-run"],
        {**env, "TRANSLATOR_APP_DATA_DIR": str(data)},
    )
    assert checked.returncode == 0, checked.stderr
    snapshot = json.loads(checked.stdout)["snapshot"]
    (connection,) = snapshot["connections"]
    assert connection["base_url"] == "https://api.example.invalid/v1"
    assert connection["throughput"]["concurrency"] == 3


def test_no_key_no_files(tmp_path: Path):
    env = {key: value for key, value in os.environ.items() if key != "TRANSLATOR_TEST_API_KEY"}

    refused = _run([*SEED, "--target", str(tmp_path / "d"), "--model", "cheap"], env)

    assert refused.returncode == 2
    assert "TRANSLATOR_TEST_API_KEY" in refused.stderr
    assert not (tmp_path / "d").exists()
