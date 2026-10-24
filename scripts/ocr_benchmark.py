"""在本机用真实图纸比一比「文字识别组件」认得准不准。只在本机运行，不上传任何文件。

    python scripts/ocr_benchmark.py OUT_DIR PDF或文件夹 [...] [--max-pages 3] [--tesseract]

* 有文字层的页：文字层就是标准答案。整页按原位替换的分辨率（200 dpi）渲染，用识别模型读
  一遍，逐行和文字层比（中文、日文行和西文行分开算）。
* 没有文字层的页（字画成线条）：没有标准答案，输出每页读到的文字和框（``*_reads.txt``、
  ``*_boxes.png``），给人看。
* ``--tesseract``：西文行也用 Tesseract 读一遍（同样装进临时目录），比一比两种引擎。

识别模型和「PDF 处理组件」装进一个临时的应用数据目录（设了 TRANSLATOR_APP_DATA_DIR 时用它），
不碰本机已装的组件和真实设置。结果全在 OUT_DIR 里（含图纸内容，别上传）；summary.json 只有数字。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
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
    summary["pages"] = len(pages_summary)
    summary["seconds_per_page"] = round(sum(p["seconds"] for p in pages_summary) / max(1, len(pages_summary)), 1)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "pages.json").write_text(json.dumps(pages_summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "mismatches.txt").write_text("\n".join(mismatches), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
