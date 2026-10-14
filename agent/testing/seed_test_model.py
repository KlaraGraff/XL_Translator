#!/usr/bin/env python3
"""Point an isolated Translator app-data dir at a test model, for CLI runs.

The command line takes no model flags: a smoke run sets
``TRANSLATOR_APP_DATA_DIR`` to a throwaway directory prepared by this script
and the engine reads its model from there, like the app would.  The API key
comes from an environment variable and is written through the app's own
``save_key``; nothing is read from or written to the user's real
``settings.json`` / ``keys.json``.

    TRANSLATOR_TEST_API_KEY=sk-… ./.venv/bin/python3 agent/testing/seed_test_model.py \\
        --target .runtime/self-tests/<slug>/app-data \\
        --provider custom_openai --base-url https://…/v1 --model <cheap-model> --concurrency 4

The target language is given per run (``translate … --to fr``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

RATE_SURFACES = ("excel", "word", "pdf_text", "cad")


def _formal_app_data_dir() -> Path:
    # app_paths honours the test override; hide it to find the real location.
    override = os.environ.pop("TRANSLATOR_APP_DATA_DIR", None)
    try:
        from core.app_paths import get_app_data_dir

        return get_app_data_dir().expanduser().resolve()
    finally:
        if override is not None:
            os.environ["TRANSLATOR_APP_DATA_DIR"] = override


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", required=True, type=Path, help="isolated app-data directory")
    parser.add_argument("--provider", default="custom_openai", help="cloud provider id (default custom_openai)")
    parser.add_argument("--model", required=True, help="text model for every text role")
    parser.add_argument("--base-url", default="", help="Base URL (OpenAI-compatible providers)")
    parser.add_argument("--key-env", default="TRANSLATOR_TEST_API_KEY", help="environment variable holding the key")
    parser.add_argument("--image-model", default="", help="PDF whole-page image model on the same connection")
    parser.add_argument("--concurrency", type=int, help="requests at once for every file type")
    args = parser.parse_args(argv)

    target = args.target.expanduser().resolve()
    if target == _formal_app_data_dir():
        parser.error("--target is the real app-data directory; use a throwaway one")
    api_key = os.environ.get(args.key_env, "").strip()
    if not api_key:
        parser.error(f"{args.key_env} is empty")

    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ["TRANSLATOR_APP_DATA_DIR"] = str(target)
    # settings resolves APP_DATA_DIR on import, so only now.
    from settings import AppSettings, load_settings, save_key, save_settings

    settings = load_settings()
    settings.engine.mode = "cloud"
    settings.engine.source_role = "independent"
    settings.engine.cloud_provider = args.provider
    settings.engine.cloud_base_url = args.base_url
    settings.engine.cloud_model = args.model
    settings.engine.connections = []  # re-seeded from the fields above
    if args.image_model:
        settings.image_model_role.cloud_model = args.image_model
        settings.image_model_role.connections = []
    if args.concurrency:
        for name in RATE_SURFACES:
            getattr(settings.rate, name).concurrency = args.concurrency
    settings = AppSettings(**settings.model_dump())
    save_settings(settings)
    save_key(args.provider, api_key, args.base_url)

    saved = load_settings()
    print(
        json.dumps(
            {
                "app_data_dir": str(target),
                "provider": saved.engine.cloud_provider,
                "model": saved.engine.cloud_model,
                "base_url": saved.engine.cloud_base_url,
                "image_model": saved.image_model_role.cloud_model,
                "concurrency": {name: getattr(saved.rate, name).concurrency for name in RATE_SURFACES},
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
