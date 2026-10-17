"""用真实历史译文实测关键信息核对规则会报多少处（只读、只统计，不改任何数据）。

用法（在仓库根目录）：
    ./.venv/bin/python3 scripts/keyfield_shadow/keyfield_shadow_run.py \
        --db "$HOME/Library/Application Support/Translator/tm.db" \
        [--find 'FARESSI|FARISSI|YUNCHONG'] [--docx 原文.docx 译文.docx]

记忆库先整份复制到输出目录再读，原库不动、应用开着也没关系。
结果写在 .runtime/self-tests/contract-keyfield-shadow/（report.md、findings.jsonl），
屏幕上只打印汇总。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import keyfield_check as k

EXAMPLES_PER_RULE = 40


def _short(text: str, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _langs(lang_pair: str) -> tuple[str, str]:
    source, _, target = str(lang_pair or "").partition("-")
    return source.lower() or "zh", target.lower()


def _copy_db(db: Path, out: Path) -> Path:
    snapshot = out / "tm_snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db) + suffix)
        if src.exists():
            shutil.copy2(src, snapshot / (db.name + suffix))
    return snapshot / db.name


def _applicability(source: str, source_lang: str) -> list[str]:
    tags = []
    if k.ID_RE.search(k.nfkc(source)):
        tags.append("含编号")
    if k.complete_dates(source, source_lang):
        tags.append("含完整日期")
    names = k.titled_names(source)
    if names:
        tags.append("含称谓人名")
        if any(k.chinese_pinyin_name(n) for n in names):
            tags.append("含拼音人名")
    return tags


def run_pairs(pairs, memory_names, findings_out, stats):
    for row_id, label, source, target, source_lang, target_lang in pairs:
        stats["rows"][label] += 1
        for tag in _applicability(source, source_lang):
            stats["applicable"][tag] += 1
        try:
            found = k.check_all(source, target, source_lang, target_lang, memory_names)
        except Exception as exc:  # noqa: BLE001 - 一条坏数据不能中断整次实测，记下来即可
            stats["errors"].append(f"{row_id}: {exc!r}")
            continue
        for item in found:
            stats["hits"][item.rule] += 1
            record = {
                "id": row_id,
                "lang_pair": label,
                "rule": item.rule,
                "message": item.message,
                "source": _short(source),
                "target": _short(target),
            }
            findings_out.write(json.dumps(record, ensure_ascii=False) + "\n")
            if len(stats["examples"][item.rule]) < EXAMPLES_PER_RULE:
                stats["examples"][item.rule].append(record)


def tm_pairs(db_copy: Path):
    conn = sqlite3.connect(str(db_copy))
    conn.row_factory = sqlite3.Row
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tm_entries)")}
    usage_col = "usage" if "usage" in columns else "'' AS usage"
    rows = conn.execute(
        f"SELECT id, source_text, target_text, lang_pair, {usage_col} FROM tm_entries"
    ).fetchall()
    conn.close()
    memory_names = {
        str(r["source_text"]).strip(): str(r["target_text"])
        for r in rows
        if r["usage"] == "always" and k.chinese_pinyin_name(str(r["source_text"]).strip())
    }
    pairs = []
    for r in rows:
        source_lang, target_lang = _langs(r["lang_pair"])
        pairs.append((f"tm:{r['id']}", r["lang_pair"], str(r["source_text"]), str(r["target_text"]),
                      source_lang, target_lang))
    return pairs, memory_names, rows


def docx_paragraphs(path: Path) -> list[str]:
    from docx import Document

    doc = Document(str(path))
    texts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                texts.extend(p.text for p in cell.paragraphs)
    return [t for t in texts if t.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path)
    parser.add_argument("--find", default="")
    parser.add_argument("--docx", nargs=2, type=Path, metavar=("SOURCE", "OUTPUT"))
    parser.add_argument("--docx-langs", default="fr-zh")
    parser.add_argument("--out", type=Path, default=Path(".runtime/self-tests/contract-keyfield-shadow"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    stats = {
        "rows": Counter(),
        "applicable": Counter(),
        "hits": Counter(),
        "examples": defaultdict(list),
        "errors": [],
    }
    found_lines: list[str] = []
    with (args.out / "findings.jsonl").open("w", encoding="utf-8") as findings_out:
        if args.db:
            db_copy = _copy_db(args.db.expanduser(), args.out)
            pairs, memory_names, rows = tm_pairs(db_copy)
            run_pairs(pairs, memory_names, findings_out, stats)
            if args.find:
                pattern = re.compile(args.find, re.IGNORECASE)
                for r in rows:
                    if pattern.search(str(r["source_text"])) or pattern.search(str(r["target_text"])):
                        found_lines.append(
                            f"- tm:{r['id']} [{r['lang_pair']}] 原文：{_short(r['source_text'], 160)} ／ "
                            f"译文：{_short(r['target_text'], 160)}"
                        )
        if args.docx:
            src_paras = docx_paragraphs(args.docx[0])
            out_paras = docx_paragraphs(args.docx[1])
            source_lang, target_lang = _langs(args.docx_langs)
            if len(src_paras) == len(out_paras):
                pairs = [(f"docx:{i + 1}", args.docx_langs, s, t, source_lang, target_lang)
                         for i, (s, t) in enumerate(zip(src_paras, out_paras))]
                run_pairs(pairs, {}, findings_out, stats)
            else:
                stats["errors"].append(
                    f"docx 段落数不同（原文 {len(src_paras)}，译文 {len(out_paras)}），未逐段比对"
                )
            if args.find:
                pattern = re.compile(args.find, re.IGNORECASE)
                for label, paras in (("原文", src_paras), ("译文", out_paras)):
                    for i, text in enumerate(paras, 1):
                        for m in pattern.finditer(text):
                            start = max(0, m.start() - 30)
                            found_lines.append(f"- docx {label} 第 {i} 段：…{_short(text[start:m.end() + 30], 100)}…")

    lines = ["# 关键信息核对实测", ""]
    lines.append("## 读了多少条")
    for label, count in sorted(stats["rows"].items()):
        lines.append(f"- {label}: {count}")
    lines.append("")
    lines.append("## 规则用得上的条数（原文里有这类内容）")
    for tag in ("含编号", "含完整日期", "含称谓人名", "含拼音人名"):
        lines.append(f"- {tag}: {stats['applicable'][tag]}")
    lines.append("")
    lines.append("## 每条规则报了几处")
    for rule in ("证件号", "日期", "人名拼写", "拼音人名"):
        lines.append(f"- {rule}: {stats['hits'][rule]}")
    for rule, examples in stats["examples"].items():
        lines.append("")
        lines.append(f"### {rule}（前 {len(examples)} 例）")
        for ex in examples:
            lines.append(f"- {ex['id']} [{ex['lang_pair']}] {ex['message']}")
            lines.append(f"  - 原文：{ex['source']}")
            lines.append(f"  - 译文：{ex['target']}")
    if found_lines:
        lines += ["", "## 按 --find 找到的条目", *found_lines[:40]]
    if stats["errors"]:
        lines += ["", "## 出错", *[f"- {e}" for e in stats["errors"][:20]]]
    report = "\n".join(lines) + "\n"
    (args.out / "report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
