#!/usr/bin/env python3
"""Word 公式补标记对比实验（反馈 #19，本机用真模型跑）。

    TRANSLATOR_APP_DATA_DIR=<真实数据目录的副本> \\
    python scripts/word_formula_experiment.py --docx 论文.docx --out ~/Downloads/反馈19_公式复核/experiment

第一步整篇翻一遍（记忆库不查，免得前几轮留下的译文直接套用），输出的 Word 放在 out/full，
记下翻译时公式记号一次带齐的段落比例和补标记的统计。第二步拿翻译时带齐了的那些段落当参照：
去掉记号，只留译文文字，按每种「每组几个 × 每次请求几个」各补几遍，看用时、请求次数、首次
放好 / 重试 / 拆开 / 放到句末的个数，以及放的位置和参照是否一致。

结果只有数字（summary.md / results.json），不含论文内容；位置不一致的例子写在
out/mismatches.txt，只在本机看。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_TOKEN_RE = re.compile(r"<\s*(/?)\s*g\s*(\d+)\s*>|<\s*x\s*(\d+)\s*/\s*>", re.IGNORECASE)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Word formula mark placement experiment")
    parser.add_argument("--docx", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--source-lang", default="en")
    parser.add_argument("--target-lang", default="zh")
    parser.add_argument("--variants", default="3x3,3x15,5x5,5x20,8x8,8x40",
                        help="每组几个 x 每次请求最多几个，逗号分隔")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--skip-full", action="store_true", help="不整篇翻，只用上次存下的参照（out/reference.json）")
    return parser.parse_args(argv)


def slot_offsets(marked: str) -> dict[int, int]:
    """每个占位符前面有几个字（不算标记本身）。"""
    offsets, plain = {}, 0
    last = 0
    for match in _TOKEN_RE.finditer(marked):
        plain += match.start() - last
        last = match.end()
        if match.group(3) is not None:
            offsets[int(match.group(3))] = plain
    return offsets


def plain_of(marked: str) -> str:
    return _TOKEN_RE.sub("", marked)


def compare(reference: str, placed: str) -> dict[str, int]:
    """placed 里每个占位符和参照比：same 位置相同；near 只隔空白、标点或 1–2 个字；far 其他；
    missing 没放。"""
    ref, got = slot_offsets(reference), slot_offsets(placed)
    text = plain_of(reference)
    result = {"same": 0, "near": 0, "far": 0, "missing": 0}
    for slot, position in ref.items():
        if slot not in got:
            result["missing"] += 1
            continue
        other = got[slot]
        if other == position:
            result["same"] += 1
            continue
        between = text[min(position, other):max(position, other)]
        stripped = re.sub(r"[\s\W_]", "", between)
        result["near" if len(stripped) <= 2 else "far"] += 1
    return result


def full_run(args, settings_module):
    from core.word_document import WordFileItem
    from core import word_task_runner as runner_module

    settings = settings_module.load_settings()
    settings.source_lang = args.source_lang
    settings.target_lang = args.target_lang
    out = args.out / "full"
    out.mkdir(parents=True, exist_ok=True)
    item = WordFileItem(path=args.docx, name=args.docx.name, size_kb=args.docx.stat().st_size / 1024)
    runner = runner_module.WordTaskRunner([item], settings, source_root=args.docx.parent, source_lang=args.source_lang)
    engines = []
    placement = {}
    real_build = runner_module.build_engine
    real_place = runner_module.place_marks

    def build(*a, **k):
        engine = real_build(*a, **k)
        engines.append(engine)
        return engine

    def place(*a, **k):
        started = time.monotonic()
        result = real_place(*a, **k)
        placement.update(k.get("stats") or {})
        placement["items"] = len(a[1]) if len(a) > 1 else len(k.get("items", []))
        placement["seconds"] = round(time.monotonic() - started, 1)
        return result

    started = time.monotonic()
    with ExitStack() as stack:
        stack.enter_context(patch.object(runner_module, "build_engine", side_effect=build))
        stack.enter_context(patch.object(runner_module, "place_marks", side_effect=place))
        stack.enter_context(patch.object(runner_module, "build_word_output_dir", return_value=out))
        stack.enter_context(patch.object(runner_module.tm_manager, "lookup_batch", return_value={}))
        runner._run()
    seconds = round(time.monotonic() - started, 1)
    mark_engine = runner._inline_mark_engine
    if mark_engine is None:
        raise SystemExit("这份文件没有带公式或局部格式的段落")
    parents = {piece: source for source, parts in mark_engine.parts.items() for piece, _m in parts}
    reference = []
    for key, (sent, translation) in mark_engine.marked_translations.items():
        if not slot_offsets(sent):
            continue
        hints = runner._slot_hints.get(parents.get(key, key), {})
        reference.append({"key": key, "marked": sent, "translation": translation,
                          "hints": {str(k): v for k, v in hints.items()}})
    outcomes = list(mark_engine.outcomes.values())
    summary = {
        "seconds": seconds,
        "marked_paragraphs": len(runner._marked_sources),
        "formula_paragraphs": sum(1 for marked in runner._marked_sources.values() if slot_offsets(marked)),
        "translated_ok": outcomes.count("ok"),
        "translated_partial": outcomes.count("partial"),
        "placement": placement,
        "flagged_paragraphs": len(runner._formula_flags),
        "flagged_formulas": sum(len(spans) for spans in runner._formula_flags.values()),
        "concurrency": int(getattr(runner._rate, "concurrency", 1) or 1),
        "retries": int(runner._word_batch.strict_retry_attempts),
        "engine": getattr(engines[0], "engine_name", "") if engines else "",
    }
    return summary, reference, (engines[0] if engines else None)


def build_engine_from_settings(settings_module):
    from core.engine_dispatcher import build_engine
    from core.surface_rate import engine_settings_for_rate, resolve_surface_rate
    from core.model_roles import ROLE_TRANSLATION, resolve_effective_model_config

    settings = settings_module.load_settings()
    model_config = resolve_effective_model_config(settings, ROLE_TRANSLATION)
    rate = resolve_surface_rate(settings, "word", mode=getattr(model_config, "mode", None))
    return build_engine(engine_settings_for_rate(settings, rate)), rate


class _CountingEngine:
    """转手给真引擎，顺便数补标记请求发出去、收回来的字数（估算花费用）。"""

    _delegates_capabilities = True

    def __init__(self, inner):
        self.inner = inner
        self.sent = self.received = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def chat(self, system, user):
        answer = self.inner.chat(system, user)
        self.sent += len(system) + len(user)
        self.received += len(answer or "")
        return answer


def stress(engine, reference, group, request, concurrency, retries, mismatches):
    from core.word_inline_marks import place_marks, unmark

    items, hints = [], {}
    for row in reference:
        plain = unmark(row["translation"], row["marked"], row["key"])
        items.append((row["key"], row["marked"], plain))
        hints[row["key"]] = {int(k): v for k, v in row["hints"].items()}
    stats, flags, reasons = {}, {}, {}
    engine = _CountingEngine(engine)
    started = time.monotonic()
    placed = place_marks(engine, items, hints=hints, retries=retries, group_size=group, request_marks=request,
                         concurrency=concurrency, flags=flags, stats=stats, reasons=reasons)
    seconds = round(time.monotonic() - started, 1)
    totals = {"same": 0, "near": 0, "far": 0, "missing": 0}
    for row in reference:
        entry = placed.get(row["key"])
        if entry is None:
            totals["missing"] += len(slot_offsets(row["marked"]))
            continue
        result = compare(row["translation"], entry[1])
        for name, count in result.items():
            totals[name] += count
        if result["far"]:
            mismatches.append(f"[{group}x{request}] 参照：{row['translation']}\n           补的：{entry[1]}\n")
    return {"seconds": seconds, **stats, **totals, "sent_chars": engine.sent, "received_chars": engine.received,
            "flagged_formulas": sum(len(spans) for spans in flags.values()),
            "unplaced_paragraphs": len(items) - len(placed)}


def main(argv=None):
    args = parse_args(argv)
    data_dir = os.environ.get("TRANSLATOR_APP_DATA_DIR")
    if not data_dir:
        raise SystemExit("请先把 TRANSLATOR_APP_DATA_DIR 设成真实数据目录的副本")
    args.out = args.out.expanduser()
    args.out.mkdir(parents=True, exist_ok=True)
    import settings as settings_module

    results = {"variants": {}}
    reference_path = args.out / "reference.json"
    engine = None
    if args.skip_full:
        reference = json.loads(reference_path.read_text(encoding="utf-8"))
        results["full"] = json.loads((args.out / "results.json").read_text(encoding="utf-8")).get("full")
    else:
        results["full"], reference, engine = full_run(args, settings_module)
        reference_path.write_text(json.dumps(reference, ensure_ascii=False), encoding="utf-8")
        print("整篇翻译：", json.dumps(results["full"], ensure_ascii=False))
    fresh, rate = build_engine_from_settings(settings_module)
    engine = engine or fresh
    concurrency = (results["full"] or {}).get("concurrency") or int(getattr(rate, "concurrency", 1) or 1)
    retries = (results["full"] or {}).get("retries") or int(rate.word_batch_settings().strict_retry_attempts)
    results["reference_paragraphs"] = len(reference)
    results["reference_formulas"] = sum(len(slot_offsets(row["marked"])) for row in reference)
    mismatches: list[str] = []
    for variant in [value.strip() for value in args.variants.split(",") if value.strip()]:
        group, request = (int(part) for part in variant.lower().split("x"))
        runs = []
        for attempt in range(args.repeat):
            row = stress(engine, reference, group, request, concurrency, retries, mismatches)
            print(f"{variant} 第 {attempt + 1} 遍：", json.dumps(row, ensure_ascii=False))
            runs.append(row)
        results["variants"][variant] = runs
        (args.out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out / "mismatches.txt").write_text("\n".join(mismatches), encoding="utf-8")
    lines = [
        "# 公式补标记对比实验",
        "",
        f"整篇翻译：{json.dumps(results['full'], ensure_ascii=False)}",
        "",
        f"参照段落 {results['reference_paragraphs']} 个，公式 {results['reference_formulas']} 个"
        f"（并发 {concurrency}，单段重试 {retries} 次）",
        "",
        "| 方案 | 遍 | 用时 s | 请求 | 发出字数 | 组 | 首次 | 重试 | 拆开 | 句末 | 位置相同 | 差一点 | 不一致 | 没放 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for variant, runs in results["variants"].items():
        for index, row in enumerate(runs, 1):
            lines.append(
                f"| {variant} | {index} | {row['seconds']} | {row.get('requests', 0)} | {row['sent_chars']} | "
                f"{row.get('groups', 0)} | "
                f"{row.get('first', 0)} | {row.get('retried', 0)} | {row.get('split', 0)} | "
                f"{row['flagged_formulas']} | {row['same']} | {row['near']} | {row['far']} | {row['missing']} |"
            )
    (args.out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
