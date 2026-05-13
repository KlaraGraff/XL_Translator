#!/usr/bin/env python3
"""Command-line entrypoint for the Word translator."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.headless_translate import build_runtime_settings  # noqa: E402
from core.headless_word_translate import run_word_translation_path  # noqa: E402
from settings import load_settings  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the local Word translator without opening the Streamlit UI.",
    )
    parser.add_argument("source", help="DOCX file path or folder path to translate.")
    parser.add_argument("--target-lang", help="Target language code or display name, such as en or 英文.")
    parser.add_argument("--source-lang", help="Source language code or display name, such as zh or 中文.")
    parser.add_argument("--output-dir", help="Optional custom output root directory.")
    parser.add_argument("--engine-mode", choices=("cloud", "local"), help="Override the saved engine mode.")
    parser.add_argument("--cloud-provider", help="Override the saved cloud provider.")
    parser.add_argument("--cloud-model", help="Override the saved cloud model.")
    parser.add_argument("--cloud-base-url", help="Override the saved cloud base URL.")
    parser.add_argument("--ollama-model", help="Override the saved Ollama model.")
    parser.add_argument("--concurrency", type=int, help="Override the saved cloud concurrency.")
    parser.add_argument("--ollama-concurrency", type=int, help="Override the saved Ollama concurrency.")
    parser.add_argument("--batch-size", type=int, help="Override the saved batch size.")
    parser.add_argument("--domain-preset", help="Override the saved domain preset.")
    parser.add_argument("--custom-prompt", help="Set a one-off custom prompt and force domain preset to 自定义.")
    parser.add_argument("--json", action="store_true", help="Print the final result as JSON.")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        settings = build_runtime_settings(
            base_settings=load_settings(),
            target_lang=args.target_lang,
            source_lang=args.source_lang,
            output_dir=args.output_dir,
        )
        _apply_runtime_overrides(settings, args)
        result = run_word_translation_path(
            args.source,
            settings=settings,
            event_handler=None if args.quiet else _print_event,
        )
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_human_summary(result.to_dict())

    return 0


def _apply_runtime_overrides(settings, args: argparse.Namespace) -> None:
    if args.engine_mode:
        settings.engine.mode = args.engine_mode
    if args.cloud_provider:
        settings.engine.cloud_provider = args.cloud_provider.strip()
    if args.cloud_model:
        settings.engine.cloud_model = args.cloud_model.strip()
    if args.cloud_base_url:
        settings.engine.cloud_base_url = args.cloud_base_url.strip()
    if args.ollama_model:
        settings.engine.ollama_model = args.ollama_model.strip()
    if args.concurrency is not None:
        settings.engine.concurrency = args.concurrency
    if args.ollama_concurrency is not None:
        settings.engine.ollama_concurrency = args.ollama_concurrency
    if args.batch_size is not None:
        settings.engine.batch_size = args.batch_size
    if args.domain_preset:
        settings.domain_preset = args.domain_preset.strip()
    if args.custom_prompt:
        settings.domain_preset = "自定义"
        settings.custom_prompt = args.custom_prompt


def _print_event(event: dict[str, object]) -> None:
    event_type = event.get("type")
    if event_type == "progress":
        print(
            (
                "[PROGRESS] "
                f"{event['phase_index']}/{event['phase_total']} "
                f"{event['phase_name']} "
                f"{event['step_done']}/{event['step_total']}"
            ),
            file=sys.stderr,
            flush=True,
        )
        return

    if event_type == "status":
        print(f"[STATUS] {event['message']}", file=sys.stderr, flush=True)
        return

    if event_type == "log":
        print(f"[{event['level']}] {event['message']}", file=sys.stderr, flush=True)
        return

    if event_type == "done":
        print(f"[DONE] 输出目录：{event['output_dir']}", file=sys.stderr, flush=True)
        return

    if event_type == "error":
        print(f"[ERROR] {event['message']}", file=sys.stderr, flush=True)
        return

    if event_type == "stopped":
        print(f"[STOPPED] {event['message']}", file=sys.stderr, flush=True)


def _print_human_summary(result: dict[str, object]) -> None:
    successful_outputs = list(result.get("successful_outputs") or [])
    print(f"源路径: {result['source_path']}")
    print(
        "语言: "
        f"{result['source_lang_display']} -> {result['target_lang_display']}"
    )
    print(f"输出目录: {result['output_dir']}")
    print(f"成功文件: {len(successful_outputs)}")
    if successful_outputs:
        print("结果文件:")
        for output_path in successful_outputs:
            print(f"- {output_path}")


if __name__ == "__main__":
    raise SystemExit(main())
