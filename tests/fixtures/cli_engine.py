"""A real engine (``api.launcher``) with test doubles, for end-to-end CLI tests.

Started as its own process, it plays the running app: it publishes
``engine.json`` in ``TRANSLATOR_APP_DATA_DIR`` and serves the same HTTP API.
Two things differ from the shipped engine:

- ``ext:sample`` (tests/fixtures/sample_surface.py) is registered, so a type
  added at run time can be driven with no code of its own;
- text translation goes to an echo engine instead of a model, so an Excel
  task runs end to end offline (``[fr] 原文``).

Never shipped: ``tests/`` is not part of the sidecar.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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
