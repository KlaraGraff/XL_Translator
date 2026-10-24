"""在本机用真实图纸比一比「文字识别组件」认得准不准。只在本机运行，不上传任何文件。

    python scripts/ocr_benchmark.py OUT_DIR PDF或文件夹 [...] [--max-pages 3] [--tesseract]

* 有文字层的页：文字层就是标准答案。整页按原位替换的分辨率（200 dpi）渲染，用识别模型读
  一遍，逐行和文字层比（中文、日文行和西文行分开算）。
* 没有文字层的页（字画成线条）：没有标准答案，输出每页读到的文字和框（``*_reads.txt``、
  ``*_boxes.png``），给人看。
* ``--tesseract``：西文行也用 Tesseract 读一遍（同样装进临时目录），比一比两种引擎。
* 每一行识别结果都按原位替换的规则分一次类（译成法文时：翻译、留给人复核、保留原样），
  算出「读错了却会被翻译」的行有多少，以及把把握门槛提高后能拦下多少（``decisions``、
  ``sweep``）。没有文字层的页按分类和原因计数，单个汉字会被翻译的，列出是哪些字。

识别模型和「PDF 处理组件」装进一个临时的应用数据目录（设了 TRANSLATOR_APP_DATA_DIR 时用它），
不碰本机已装的组件和真实设置。结果全在 OUT_DIR 里（含图纸内容，别上传）；summary.json 只有数字
（和会被翻译的单个汉字）。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DPI = 200
_CJK = re.compile(r"[぀-ヿ㐀-鿿]")


def _norm(text: str) -> str:
    text = re.sub("[‐‑‒–—−]", "-", unicodedata.normalize("NFKC", text))
    return re.sub(r"\s+", "", text).casefold()


def _lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _inter(a, b) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def _area(b) -> float:
    return max(1.0, (b[2] - b[0]) * (b[3] - b[1]))


def _matched_text(truth: dict, rows: list[dict]) -> str:
    box = truth["display_box"]
    hits = [r for r in rows if _inter(r["display_box"], box) >= 0.5 * min(_area(r["display_box"]), _area(box))]
    rot = truth.get("display_rotation", 0)
    if rot == 90:
        hits.sort(key=lambda r: -r["display_box"][3])
    elif rot == 270:
        hits.sort(key=lambda r: r["display_box"][1])
    else:
        hits.sort(key=lambda r: (round(r["display_box"][1] / 20), r["display_box"][0]))
    return "".join(r["source_text"] for r in hits)


def _overlapping(row: dict, truth: list[dict]) -> list[dict]:
    box = row["display_box"]
    return [t for t in truth if _inter(t["display_box"], box) >= 0.5 * min(_area(t["display_box"]), _area(box))]


def _min_char(row: dict) -> float:
    words = row.get("words") or []
    probs = [w["score"] for w in words if _CJK.search(w["text"])] or [w["score"] for w in words]
    return min(probs) if probs else float(row.get("ocr_score") or 0.0)


def _decide(rows: list[dict], lang: str, target: str) -> None:
    """Each row's decision under the in-place rules (core.pdf_vector.classify), no model call."""
    from core.pdf_vector.classify import classify, harmonize
    from core.pdf_vector.common import norm_text

    harmonize(rows, lang)
    strong = {norm_text(r["source_text"]).casefold() for r in rows
              if (r.get("ocr_score") or 0) >= 0.9 and int(r.get("display_rotation", 0)) == 0}
    for r in rows:
        r["decision"], r["decision_reason"] = classify(r, 0.45, lang, strong, target)


SWEEP = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95)


def _install(tesseract: bool) -> None:
    from core import ocr_component_install, pdf_component_install, ppocr_component_install

    if pdf_component_install.installed_site_dir() is None:
        print("installing the PDF component …", flush=True)
        pdf_component_install.install()
    if ppocr_component_install.installed_dirs() is None:
        print("installing the recognition models …", flush=True)
        ppocr_component_install.install()
    if tesseract and ocr_component_install.installed_executable() is None and ocr_component_install.manifest():
        print("installing Tesseract …", flush=True)
        ocr_component_install.install()


def _pdfs(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for item in paths:
        path = Path(item).expanduser()
        out += sorted(path.rglob("*.pdf")) if path.is_dir() else [path]
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out")
    parser.add_argument("pdfs", nargs="+")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--tesseract", action="store_true")
    args = parser.parse_args(argv)
    if not os.environ.get("TRANSLATOR_APP_DATA_DIR"):
        os.environ["TRANSLATOR_APP_DATA_DIR"] = tempfile.mkdtemp(prefix="ocr-benchmark-")
    _install(args.tesseract)

    from PIL import Image, ImageDraw

    from core.pdf_component import require_fitz
    from core.pdf_vector import extract
    from core.ppocr_component import make_reader, shutdown_reader

    fitz = require_fitz()
    reader = make_reader()
    if reader is None:
        print("the recognition models are not usable here")
        return 1
    tesseract = None
    if args.tesseract:
        from core.ocr_engine import resolve_tesseract

        exe = resolve_tesseract()
        tesseract = str(exe) if exe else None
        print(f"tesseract: {tesseract or 'not found'}")
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    totals: dict[str, dict[str, float]] = {}
    pages_summary = []
    mismatches = []
    judged: list[dict] = []          # rows of text-layer pages with their decision and whether they read right
    blind: dict[str, int] = {}       # rows of pages without a text layer: decision / reason counts
    single: dict[str, int] = {}      # lone Chinese / Japanese characters that would be translated

    from core.pdf_vector.runner import guess_language

    def add(kind: str, exact: bool, acc: float) -> None:
        total = totals.setdefault(kind, {"lines": 0, "exact": 0, "char_acc": 0.0})
        total["lines"] += 1
        total["exact"] += int(exact)
        total["char_acc"] += acc

    for pdf in _pdfs(args.pdfs):
        try:
            doc = fitz.open(pdf)
        except Exception as exc:  # noqa: BLE001 - skip unreadable files
            print(f"skip {pdf}: {exc}")
            continue
        for pno in range(min(len(doc), args.max_pages)):
            page = doc[pno]
            tag = f"{len(pages_summary) + 1:03d}"
            truth = [r for r in extract.native_rows(page, DPI)
                     if sum(c.isalnum() for c in r["source_text"]) >= 2 and not r.get("oblique")]
            pix = page.get_pixmap(matrix=fitz.Matrix(DPI / 72, DPI / 72), alpha=False)
            gray = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
            started = time.monotonic()
            rows, _dropped = extract.dedup(reader(gray))
            seconds = time.monotonic() - started
            # the same steps as 原位替换 after reading: join letter-spaced labels, split at
            # table rules, join paragraphs
            V, Hs = extract.rules(page, DPI)
            done = extract.join_spaced_cjk(copy.deepcopy(rows), [], V, Hs)
            done, _n = extract.split_at_rules(done, V, Hs)
            done = extract.group_paragraphs(done, V, Hs)
            lang = guess_language([r["source_text"] for r in truth] or [r["source_text"] for r in done],
                                  fallback="zh")
            _decide(done, lang, "fr")
            for r in done:
                kind = "cjk" if _CJK.search(r["source_text"]) else "latin"
                letters = [c for c in r["source_text"] if c.isalpha()]
                if truth:
                    hits = _overlapping(r, truth)
                    want = "".join(_norm(t["source_text"]) for t in sorted(
                        hits, key=lambda t: (round(t["display_box"][1] / 20), t["display_box"][0])))
                    judged.append({"kind": kind, "decision": r["decision"], "reason": r["decision_reason"],
                                   "right": (_norm(r["source_text"]) == want) if hits else None,
                                   "score": float(r.get("ocr_score") or 0.0), "min_char": _min_char(r),
                                   "single": len(letters) == 1})
                else:
                    blind[f"{kind} {r['decision']} {r['decision_reason']}"] = \
                        blind.get(f"{kind} {r['decision']} {r['decision_reason']}", 0) + 1
                    if r["decision"] == "translate" and len(letters) == 1 and _CJK.search(letters[0]):
                        single[letters[0]] = single.get(letters[0], 0) + 1
            tess_rows = []
            if tesseract and truth:
                raw = extract.ocr_rows(gray, "fra+eng", (0, 90, 270), max(2, (os.cpu_count() or 4) - 2), tesseract)
                tess_rows, _dropped = extract.dedup(raw)
            info = {"page": tag, "size_px": list(gray.size), "seconds": round(seconds, 1),
                    "text_layer_lines": len(truth), "ocr_rows": len(rows)}
            for line in truth:
                kind = "cjk" if _CJK.search(line["source_text"]) else "latin"
                want = _norm(line["source_text"])
                got = _matched_text(line, rows)
                acc = max(0.0, 1 - _lev(want, _norm(got)) / max(1, len(want)))
                add(kind, _norm(got) == want, acc)
                if _norm(got) != want:
                    mismatches.append(f"{tag} {kind} {acc:.2f}  {line['source_text']!r} -> {got!r}")
                if tess_rows and kind == "latin":
                    tess = _matched_text(line, tess_rows)
                    tacc = max(0.0, 1 - _lev(want, _norm(tess)) / max(1, len(want)))
                    add("latin_tesseract", _norm(tess) == want, tacc)
            if not truth:
                # 没有文字层：给人看读到了什么、在哪里
                (out / f"{tag}_reads.txt").write_text(
                    "\n".join(f"{r['display_rotation']:>3} {r.get('ocr_score', 0):.2f}  {r['source_text']}" for r in rows),
                    encoding="utf-8")
                overlay = gray.convert("RGB")
                draw = ImageDraw.Draw(overlay)
                for r in rows:
                    draw.rectangle(r["display_box"], outline=(220, 0, 0), width=3)
                overlay.save(out / f"{tag}_boxes.png")
            pages_summary.append(info)
            print(f"{tag}: {info}", flush=True)
        doc.close()
    shutdown_reader()
    summary = {kind: {"lines": int(t["lines"]), "exact": round(t["exact"] / t["lines"], 3),
                      "char_acc": round(t["char_acc"] / t["lines"], 3)}
               for kind, t in totals.items() if t["lines"]}
    # 读对 / 读错 × 翻译 / 复核 / 保留：「读错了却会被翻译」是要压下去的那一格
    decisions: dict[str, dict[str, int]] = {}
    for j in judged:
        right = {True: "right", False: "wrong", None: "no_text_layer_line"}[j["right"]]
        cell = decisions.setdefault(j["kind"], {})
        cell[f"{j['decision']}_{right}"] = cell.get(f"{j['decision']}_{right}", 0) + 1
    summary["decisions"] = decisions
    # 只对会被翻译的行：把把握门槛提到 t 以后，还会翻译的读对 / 读错各多少
    sweep: dict[str, dict[str, list[int]]] = {}
    for kind in ("cjk", "latin"):
        rows_k = [j for j in judged if j["kind"] == kind and j["decision"] == "translate" and j["right"] is not None]
        for field in ("min_char", "score"):
            sweep[f"{kind}_{field}"] = {
                str(t): [sum(1 for j in rows_k if j[field] >= t and j["right"]),
                         sum(1 for j in rows_k if j[field] >= t and not j["right"])] for t in SWEEP}
    summary["sweep_right_wrong"] = sweep
    summary["single_char_translated_on_text_layer_pages"] = dict(Counter(
        "right" if j["right"] else "wrong" if j["right"] is False else "no_line"
        for j in judged if j["single"] and j["kind"] == "cjk" and j["decision"] == "translate"))
    summary["no_text_layer_pages"] = dict(sorted(blind.items(), key=lambda kv: -kv[1]))
    summary["no_text_layer_single_chars_translated"] = dict(sorted(single.items(), key=lambda kv: -kv[1]))
    summary["pages"] = len(pages_summary)
    summary["seconds_per_page"] = round(sum(p["seconds"] for p in pages_summary) / max(1, len(pages_summary)), 1)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "pages.json").write_text(json.dumps(pages_summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "mismatches.txt").write_text("\n".join(mismatches), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
