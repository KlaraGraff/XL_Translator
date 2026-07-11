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

from config import (  # noqa: E402
    WORD_BATCH_CHARS_MAX,
    WORD_BATCH_CHARS_MIN,
    WORD_BATCH_PARAGRAPHS_MAX,
    WORD_BATCH_PARAGRAPHS_MIN,
    WORD_BATCH_SPLIT_CHARS_MAX,
    WORD_BATCH_SPLIT_CHARS_MIN,
    WORD_REVIEW_HIGHLIGHT_COLOR_DEFAULT,
    WORD_STRICT_RETRY_ATTEMPTS_MAX,
    WORD_STRICT_RETRY_ATTEMPTS_MIN,
)
from core.headless_translate import build_runtime_settings  # noqa: E402
from core.headless_word_translate import run_word_translation_path  # noqa: E402
from settings import load_settings, set_cloud_provider_config  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the local Word translator without opening the desktop UI.",
    )
    parser.add_argument("source", help="DOCX/DOC file path or folder path to translate.")
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
    parser.add_argument("--batch-size", type=int, help="Alias for --word-batch-paragraphs.")
    parser.add_argument("--word-batch-paragraphs", type=int, help="Override Word paragraphs per request batch.")
    parser.add_argument("--word-batch-chars", type=int, help="Override Word character budget per request batch.")
    parser.add_argument("--word-split-chars", type=int, help="Override the long-paragraph split threshold.")
    parser.add_argument("--word-retry-attempts", type=int, help="Override Word strict retry attempts for unresolved paragraphs.")
    parser.add_argument(
        "--word-untranslated-only",
        action="store_true",
        help="Only insert translations at source-only positions in an already bilingual Word document.",
    )
    parser.add_argument(
        "--word-protect-scheme-cover",
        action="store_true",
        help="Protect method-statement covers while allowing the foreign title below the Chinese scheme title to be translated.",
    )
    highlight_group = parser.add_mutually_exclusive_group()
    highlight_group.add_argument(
        "--word-highlight-review",
        dest="word_highlight_review",
        action="store_true",
        help="Highlight unresolved review items in generated Word files.",
    )
    highlight_group.add_argument(
        "--word-no-highlight-review",
        dest="word_highlight_review",
        action="store_false",
        help="Disable unresolved review highlighting in generated Word files.",
    )
    parser.set_defaults(word_highlight_review=None)
    parser.add_argument("--word-highlight-color", help="Review highlight color as a 6-digit hex value, such as FFF2CC.")
    native_group = parser.add_mutually_exclusive_group()
    native_group.add_argument(
        "--word-doc-prefer-native",
        dest="word_doc_prefer_native",
        action="store_true",
        help="Prefer local Microsoft Word when converting legacy .doc files.",
    )
    native_group.add_argument(
        "--word-doc-no-native",
        dest="word_doc_prefer_native",
        action="store_false",
        help="Skip local Microsoft Word and use compatible .doc conversion fallbacks.",
    )
    parser.set_defaults(word_doc_prefer_native=None)
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
            untranslated_only=args.word_untranslated_only or args.word_protect_scheme_cover,
            protect_scheme_cover=args.word_protect_scheme_cover,
            event_handler=None if args.quiet else _print_event,
        )
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_human_summary(result.to_dict())

    return _result_exit_code(result.file_results)


def _result_exit_code(file_results: list[dict[str, object]]) -> int:
    return 0 if any(item.get("success") for item in file_results) else 1


def _apply_runtime_overrides(settings, args: argparse.Namespace) -> None:
    if args.engine_mode:
        settings.engine.mode = args.engine_mode
    if args.cloud_provider:
        settings.engine.cloud_provider = args.cloud_provider.strip()
        if not args.cloud_base_url:
            settings.engine.cloud_base_url = ""
    if args.cloud_model:
        settings.engine.cloud_model = args.cloud_model.strip()
    if args.cloud_base_url:
        settings.engine.cloud_base_url = args.cloud_base_url.strip()
    if args.cloud_provider or args.cloud_model or args.cloud_base_url:
        set_cloud_provider_config(
            settings.engine,
            settings.engine.cloud_provider,
            cloud_model=settings.engine.cloud_model,
            cloud_base_url=settings.engine.cloud_base_url,
        )
    if args.ollama_model:
        settings.engine.ollama_model = args.ollama_model.strip()
    if args.concurrency is not None:
        settings.engine.concurrency = args.concurrency
    if args.ollama_concurrency is not None:
        settings.engine.ollama_concurrency = args.ollama_concurrency
    if args.batch_size is not None:
        settings.word_batch.max_paragraphs_per_batch = _clamp(
            args.batch_size,
            WORD_BATCH_PARAGRAPHS_MIN,
            WORD_BATCH_PARAGRAPHS_MAX,
        )
    if args.word_batch_paragraphs is not None:
        settings.word_batch.max_paragraphs_per_batch = _clamp(
            args.word_batch_paragraphs,
            WORD_BATCH_PARAGRAPHS_MIN,
            WORD_BATCH_PARAGRAPHS_MAX,
        )
    if args.word_batch_chars is not None:
        settings.word_batch.max_chars_per_batch = _clamp(
            args.word_batch_chars,
            WORD_BATCH_CHARS_MIN,
            WORD_BATCH_CHARS_MAX,
        )
    if args.word_split_chars is not None:
        settings.word_batch.split_paragraph_chars = max(
            settings.word_batch.max_chars_per_batch,
            _clamp(
                args.word_split_chars,
                WORD_BATCH_SPLIT_CHARS_MIN,
                WORD_BATCH_SPLIT_CHARS_MAX,
            ),
        )
    if args.word_retry_attempts is not None:
        settings.word_batch.strict_retry_attempts = _clamp(
            args.word_retry_attempts,
            WORD_STRICT_RETRY_ATTEMPTS_MIN,
            WORD_STRICT_RETRY_ATTEMPTS_MAX,
        )
    if args.word_highlight_review is not None:
        settings.word_review.highlight_unresolved = bool(args.word_highlight_review)
    if args.word_highlight_color:
        settings.word_review.highlight_color = _normalize_hex_color(
            args.word_highlight_color,
            fallback=WORD_REVIEW_HIGHLIGHT_COLOR_DEFAULT,
        )
    if args.word_doc_prefer_native is not None:
        settings.word_conversion.prefer_native_word = bool(args.word_doc_prefer_native)
    if args.domain_preset:
        settings.domain_preset = args.domain_preset.strip()
    if args.custom_prompt:
        settings.domain_preset = "自定义"
        settings.custom_prompt = args.custom_prompt
    settings.word_batch.split_paragraph_chars = max(
        settings.word_batch.max_chars_per_batch,
        settings.word_batch.split_paragraph_chars,
    )


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def _normalize_hex_color(value: str, *, fallback: str) -> str:
    cleaned = str(value or "").strip().lstrip("#").upper()
    if len(cleaned) == 6 and all(char in "0123456789ABCDEF" for char in cleaned):
        return cleaned
    return fallback


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
        if event.get("report_path"):
            print(f"[DONE] 质量报告：{event['report_path']}", file=sys.stderr, flush=True)
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
    if result.get("report_path"):
        print(f"质量报告: {result['report_path']}")
    print(f"成功文件: {len(successful_outputs)}")
    issues = list(result.get("issues") or [])
    resolved_count = sum(1 for issue in issues if issue.get("severity") == "resolved")
    review_count = len(issues) - resolved_count
    if issues:
        print(f"质量提示: 需复核 {review_count}，已自动处理 {resolved_count}")
    if successful_outputs:
        print("结果文件:")
        for output_path in successful_outputs:
            print(f"- {output_path}")


if __name__ == "__main__":
    raise SystemExit(main())
