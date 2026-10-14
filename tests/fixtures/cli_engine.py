"""A real engine (``api.launcher``) with test doubles, for end-to-end CLI tests.

Started as its own process (``AppEngine`` below), it plays the running app:
it publishes ``engine.json`` in ``TRANSLATOR_APP_DATA_DIR`` and serves the
same HTTP API.
Two things differ from the shipped engine:

- ``ext:sample`` (tests/fixtures/sample_surface.py) is registered, so a type
  added at run time can be driven with no code of its own;
- text translation goes to an echo engine instead of a model, so an Excel
  task runs end to end offline (``[fr] 原文``).

Never shipped: ``tests/`` is not part of the sidecar.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Starts this file as an engine (what ``engine_client.engine_command`` would run).
FIXTURE_ENGINE = [sys.executable, str(Path(__file__).resolve())]


class EchoEngine:
    """Answers every batch with ``[<target>] <source>``."""

    engine_name = "cli-test/echo"

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        return {text: f"[{target_lang}] {text}" for text in texts}

    def translate_batch_with_sources(self, texts, target_lang, system_prompt, source_lang="auto"):
        from core.language_preflight import TranslationLanguageResult

        return [
            TranslationLanguageResult(text, f"[{target_lang}] {text}", source_lang="zh", target_lang=target_lang)
            for text in texts
        ]

    def chat(self, system: str, user: str) -> str:
        raise NotImplementedError


class AppEngine:
    """This engine started as the open app, publishing ``engine.json`` in ``data_dir``."""

    def __init__(self, data_dir: Path, *, delay: float = 0.0) -> None:
        import httpx

        from core import engine_client, engine_file

        env = {**os.environ, "TRANSLATOR_APP_DATA_DIR": str(data_dir), "CLI_TEST_SAMPLE_DELAY": str(delay)}
        env.pop("TRANSLATOR_SIDECAR_PARENT_PID", None)
        self.process = subprocess.Popen(
            FIXTURE_ENGINE, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        port, token = engine_client.parse_handshake(self.process.stdout.readline().decode())
        self.http = httpx.Client(
            base_url=f"http://127.0.0.1:{port}", headers={"X-Translator-Token": token}, trust_env=False
        )
        deadline = time.monotonic() + 10
        while engine_file.read_engine_file(data_dir) is None and time.monotonic() < deadline:
            time.sleep(0.05)

    def wait_terminal(self, task_id: str, timeout: float = 30) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.http.get(f"/api/tasks/{task_id}").json()
            if status.get("terminal"):
                return status
            time.sleep(0.1)
        raise TimeoutError(task_id)

    def close(self) -> None:
        import httpx

        try:
            self.http.post("/api/system/shutdown", timeout=2)
        except httpx.HTTPError:
            pass
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.http.close()
        self.process.stdout.close()


def main() -> None:
    from core.api_config_check import ApiConfigCheckResult
    from tests.fixtures.sample_surface import register_sample_surface

    register_sample_surface(delay=float(os.environ.get("CLI_TEST_SAMPLE_DELAY") or 0))
    ok = ApiConfigCheckResult(ok=True)
    for target in (
        patch("api.surfaces.base.check_translation_api_config", return_value=ok),
        patch("core.task_runner.check_translation_api_config", return_value=ok),
        patch("core.task_runner.build_engine", return_value=EchoEngine()),
        patch("core.task_runner.resolve_effective_model_config", return_value=object()),
    ):
        target.start()

    from api.launcher import main as launch

    launch()


if __name__ == "__main__":
    main()
