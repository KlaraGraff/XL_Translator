#!/usr/bin/env python3
"""Command-line entrypoint for PDF image-layout translation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.headless_pdf_translate import (  # noqa: E402
    build_pdf_runtime_settings,
    run_pdf_translation_path,
)
from core.model_roles import SOURCE_INDEPENDENT  # noqa: E402
from settings import load_settings, set_cloud_provider_config  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run PDF image-layout translation without opening the desktop UI.",
    )
    parser.add_argument("source", help="PDF file, image file, or folder path to translate.")
    parser.add_argument("--target-lang", help="Target language code or display name, such as zh or 中文.")
    parser.add_argument("--output-dir", help="Optional custom output root directory.")
    parser.add_argument(
        "--include-images",
        action="store_true",
        help="Also translate supported image files when the source is a folder.",
    )
    parser.add_argument(
        "--pdf-page-concurrency",
        type=int,
        help="Override PDF page image-generation concurrency.",
    )
    parser.add_argument("--image-provider", help="Override the PDF image model provider.")
    parser.add_argument("--image-model", help="Override the PDF image model name.")
    parser.add_argument("--image-base-url", help="Override the PDF image model Base URL.")
    parser.add_argument("--review-provider", help="Override the PDF review model provider.")
    parser.add_argument("--review-model", help="Override the PDF review model name.")
    parser.add_argument("--review-base-url", help="Override the PDF review model Base URL.")
    parser.add_argument(
        "--pdf-retry-attempts",
        type=int,
        help="Override page retry attempts after the first generation attempt.",
    )
    review_group = parser.add_mutually_exclusive_group()
    review_group.add_argument(
        "--pdf-review",
        dest="pdf_review",
        action="store_true",
        help="Enable PDF translation review before accepting page images.",
    )
    review_group.add_argument(
        "--no-pdf-review",
        dest="pdf_review",
        action="store_false",
        help="Disable PDF translation review.",
    )
    parser.set_defaults(pdf_review=None)
    compression_group = parser.add_mutually_exclusive_group()
    compression_group.add_argument(
        "--compressed-pdf",
        dest="compressed_pdf",
        action="store_true",
        help="Generate both high-quality and compressed PDF variants.",
    )
    compression_group.add_argument(
        "--no-compressed-pdf",
        dest="compressed_pdf",
        action="store_false",
        help="Generate only the high-quality PDF variant.",
    )
    parser.set_defaults(compressed_pdf=None)
    parser.add_argument("--json", action="store_true", help="Print the final result as JSON.")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        settings = build_pdf_runtime_settings(
            base_settings=load_settings(),
            target_lang=args.target_lang,
            output_dir=args.output_dir,
            page_concurrency=args.pdf_page_concurrency,
            retry_attempts=args.pdf_retry_attempts,
            review_enabled=args.pdf_review,
            compressed_pdf=args.compressed_pdf,
        )
        _apply_runtime_overrides(settings, args)
        result = run_pdf_translation_path(
            args.source,
            settings=settings,
            include_images=args.include_images,
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
    if args.image_provider:
        settings.image_model_role.source_role = SOURCE_INDEPENDENT
        settings.image_model_role.cloud_provider = args.image_provider.strip()
        if not args.image_base_url:
            settings.image_model_role.cloud_base_url = ""
    if args.image_model:
        settings.image_model_role.source_role = SOURCE_INDEPENDENT
        settings.image_model_role.cloud_model = args.image_model.strip()
    if args.image_base_url:
        settings.image_model_role.source_role = SOURCE_INDEPENDENT
        settings.image_model_role.cloud_base_url = args.image_base_url.strip()
    if args.image_provider or args.image_model or args.image_base_url:
        set_cloud_provider_config(
            settings.image_model_role,
            settings.image_model_role.cloud_provider,
            cloud_model=settings.image_model_role.cloud_model,
            cloud_base_url=settings.image_model_role.cloud_base_url,
        )

    if args.review_provider:
        settings.pdf_review_model_role.source_role = SOURCE_INDEPENDENT
        settings.pdf_review_model_role.cloud_provider = args.review_provider.strip()
        if not args.review_base_url:
            settings.pdf_review_model_role.cloud_base_url = ""
    if args.review_model:
        settings.pdf_review_model_role.source_role = SOURCE_INDEPENDENT
        settings.pdf_review_model_role.cloud_model = args.review_model.strip()
    if args.review_base_url:
        settings.pdf_review_model_role.source_role = SOURCE_INDEPENDENT
        settings.pdf_review_model_role.cloud_base_url = args.review_base_url.strip()
    if args.review_provider or args.review_model or args.review_base_url:
        set_cloud_provider_config(
            settings.pdf_review_model_role,
            settings.pdf_review_model_role.cloud_provider,
            cloud_model=settings.pdf_review_model_role.cloud_model,
            cloud_base_url=settings.pdf_review_model_role.cloud_base_url,
        )


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
            print(f"[DONE] PDF 报告：{event['report_path']}", file=sys.stderr, flush=True)
        return

    if event_type == "error":
        print(f"[ERROR] {event['message']}", file=sys.stderr, flush=True)
        return

    if event_type == "stopped":
        print(f"[STOPPED] {event['message']}", file=sys.stderr, flush=True)


def _print_human_summary(result: dict[str, object]) -> None:
    successful_outputs = list(result.get("successful_outputs") or [])
    print(f"源路径: {result['source_path']}")
    print(f"目标语言: {result['target_lang_display']}")
    print(f"输出目录: {result['output_dir']}")
    if result.get("report_path"):
        print(f"PDF 报告: {result['report_path']}")
    if result.get("manifest_path"):
        print(f"PDF 清单: {result['manifest_path']}")
    print(f"已生成文件: {len(successful_outputs)}")
    if int(result.get("issue_count") or 0):
        print(f"质量提示: {result['issue_count']} 项需查看 PDF 报告")
    if successful_outputs:
        print("结果文件:")
        for output_path in successful_outputs:
            print(f"- {output_path}")


if __name__ == "__main__":
    raise SystemExit(main())
