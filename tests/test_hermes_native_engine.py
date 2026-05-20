from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _clear_project_modules() -> None:
    prefixes = (
        "config",
        "settings",
        "core",
        "engines",
    )
    for name in list(sys.modules):
        if name == "app_meta":
            sys.modules.pop(name, None)
            continue
        if name.startswith(prefixes):
            sys.modules.pop(name, None)


class HermesNativeEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name) / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.env_patch = patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "USERPROFILE": str(self.home),
            },
        )
        self.env_patch.start()
        self.home = Path.home()
        _clear_project_modules()
        from core.app_paths import get_app_data_dir

        self.app_data_dir = get_app_data_dir()
        shutil.rmtree(self.app_data_dir, ignore_errors=True)
        shutil.rmtree(self.home / ".hermes", ignore_errors=True)
        self.hermes_home = self.home / ".hermes"
        self.hermes_home.mkdir(parents=True, exist_ok=True)
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("OPENAI_API_KEY_BACKUP", None)

    def tearDown(self) -> None:
        _clear_project_modules()
        self.env_patch.stop()
        self.temp_dir.cleanup()

    def _write_hermes_config(self) -> None:
        (self.hermes_home / "config.yaml").write_text(
            """
model:
  default: gpt-5.4
  provider: custom
  base_url: https://primary.example/v1
  api_key_env: OPENAI_API_KEY
  api_mode: codex_responses
fallback_providers:
- provider: custom
  model: gpt-5.4
  base_url: https://backup.example/v1
  api_key_env: OPENAI_API_KEY_BACKUP
  api_mode: chat_completions
agent:
  max_turns: 120
""".strip()
            + "\n",
            encoding="utf-8",
        )

    def _write_hermes_env(self) -> None:
        (self.hermes_home / ".env").write_text(
            "OPENAI_API_KEY=primary-secret\nOPENAI_API_KEY_BACKUP=backup-secret\n",
            encoding="utf-8",
        )

    def _write_inline_hermes_config(self) -> None:
        (self.hermes_home / "config.yaml").write_text(
            """
model:
  default: mimo-v2.5-pro
  provider: xiaomimimo
  base_url: https://token-plan-cn.xiaomimimo.com/v1
  api_mode: openai
  api_key: inline-secret
""".strip()
            + "\n",
            encoding="utf-8",
        )

    def test_load_settings_defaults_to_openai_compatible_without_packaged_keys(self) -> None:
        from settings import load_settings

        settings = load_settings()

        self.assertEqual(settings.engine.cloud_provider, "custom_openai")
        self.assertFalse((self.app_data_dir / "keys.json").exists())

    def test_load_hermes_runtime_routes_reads_config_and_env(self) -> None:
        self._write_hermes_config()
        self._write_hermes_env()

        from engines.hermes_engine import load_hermes_runtime_routes

        routes = load_hermes_runtime_routes()

        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].provider, "custom")
        self.assertEqual(routes[0].model, "gpt-5.4")
        self.assertEqual(routes[0].base_url, "https://primary.example/v1")
        self.assertEqual(routes[0].api_mode, "codex_responses")
        self.assertEqual(routes[0].api_key, "primary-secret")

    def test_load_hermes_runtime_routes_accepts_inline_api_key(self) -> None:
        self._write_inline_hermes_config()

        from engines.hermes_engine import load_hermes_runtime_routes

        routes = load_hermes_runtime_routes()

        self.assertEqual(routes[0].provider, "xiaomimimo")
        self.assertEqual(routes[0].model, "mimo-v2.5-pro")
        self.assertEqual(routes[0].base_url, "https://token-plan-cn.xiaomimimo.com/v1")
        self.assertEqual(routes[0].api_key, "inline-secret")

    def test_build_engine_accepts_hermes_provider(self) -> None:
        self._write_hermes_config()
        self._write_hermes_env()

        from core.engine_dispatcher import build_engine
        from settings import AppSettings, EngineSettings

        settings = AppSettings(engine=EngineSettings(cloud_provider="hermes"))
        engine = build_engine(settings)

        self.assertTrue(engine.engine_name.startswith("hermes/"))

    def test_hermes_engine_ignores_fallback_route_config(self) -> None:
        self._write_hermes_config()
        self._write_hermes_env()

        from engines.hermes_engine import HermesEngine

        class _PrimarySuccessEngine:
            def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
                return {text: f"primary::{text}" for text in texts}

            def chat(self, system, user):
                return "primary-chat"

        built_routes: list[str] = []

        def _fake_builder(route):
            built_routes.append(route.base_url)
            return _PrimarySuccessEngine()

        with patch("engines.hermes_engine._build_route_engine", side_effect=_fake_builder):
            engine = HermesEngine()
            result = engine.translate_batch(["条目A"], "fr", "system")

        self.assertEqual(result, {"条目A": "primary::条目A"})
        self.assertEqual(built_routes, ["https://primary.example/v1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
