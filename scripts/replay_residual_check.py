#!/usr/bin/env python3
"""对已产出的双语 docx/xlsx 做残留体检（只验不译，0 API）。

用法：
    python scripts/replay_residual_check.py --target-lang fr 文件或目录 [...]

来历见 docs/redesign/2026-08-14_residual_repair_pipeline.md §六.3。
--fail-on-findings 供回放护栏使用：语料应全 clean 时，任何发现都算失败。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.residual_replay import replay_file  # noqa: E402


def _collect_paths(raw_paths: list[str]) -> list[Path]:
    collected: list[Path] = []
    for raw in raw_paths:
        path = Path(raw)
        if path.is_dir():
            collected.extend(sorted(path.rglob("*.docx")))
            collected.extend(sorted(path.rglob("*.xlsx")))
        else:
            collected.append(path)
    return [p for p in collected if not p.name.startswith("~$")]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay residual-Chinese checks over existing bilingual documents.",
    )
    parser.add_argument("paths", nargs="+", help="双语 docx/xlsx 文件或目录")
    parser.add_argument(
        "--target-lang", required=True, help="译文语言代码，如 fr / en"
    )
    parser.add_argument(
        "--source-lang", default="zh", help="源语言代码（默认 zh）"
    )
    parser.add_argument(
        "--json", action="store_true", help="以 JSON 输出（供机器消费）"
    )
    parser.add_argument(
        "--fail-on-findings",
        action="store_true",
        help="有任何发现即以退出码 1 结束（回放护栏用）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = _collect_paths(args.paths)
    if not paths:
        print("no .docx/.xlsx files found", file=sys.stderr)
        return 2

    total_findings = 0
    total_outliers = 0
    json_payload = []
    for path in paths:
        try:
            result = replay_file(
                path, target_lang=args.target_lang, source_lang=args.source_lang
            )
        except Exception as exc:  # 文件坏了不该拖垮整批体检
            print(f"!! {path}: {exc}", file=sys.stderr)
            total_findings += 1
            continue
        total_findings += len(result.findings)
        total_outliers += len(result.heading_outliers)
        if args.json:
            json_payload.append(
                {
                    "path": result.path,
                    "kind": result.kind,
                    "pair_count": result.pair_count,
                    "convention": result.convention,
                    "findings": [
                        {
                            "source_anchor": f.source_anchor,
                            "output_anchor": f.output_anchor,
                            "category": f.category,
                            "span": f.span_text,
                            "deterministic_fix": f.deterministic_fix,
                        }
                        for f in result.findings
                    ],
                    "heading_outliers": [
                        {
                            "unit_id": o.unit_id,
                            "target_text": o.target_text,
                            "majority_form": o.majority_form,
                            "fix": o.fix,
                        }
                        for o in result.heading_outliers
                    ],
                    "state_counts": result.ledger.counts(),
                }
            )
            continue
        print(
            f"== {Path(result.path).name}  配对 {result.pair_count} 段"
            f"  惯例 {result.convention}  残留 {len(result.findings)}"
            f"  标题离群 {len(result.heading_outliers)}"
        )
        for f in result.findings:
            fix_note = f"  可修复 -> {f.deterministic_fix}" if f.deterministic_fix else ""
            print(f"   [{f.source_anchor}] {f.category} «{f.span_text}»{fix_note}")
        for o in result.heading_outliers:
            fix_note = f"  可修复 -> {o.fix}" if o.fix else "（只报告）"
            print(f"   [{o.unit_id}] 标题写法偏离多数派 {o.majority_form}{fix_note}")

    if args.json:
        print(json.dumps(json_payload, ensure_ascii=False, indent=2))
    else:
        print(f"共 {len(paths)} 份文件：残留 {total_findings} 处，标题离群 {total_outliers} 处")
    if args.fail_on_findings and (total_findings or total_outliers):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
