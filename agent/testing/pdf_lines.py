"""列出 PDF 某一页上的每一行字：位置、字号、文字。只测量。

用法（仓库根目录，用软件自己的 Python）：

    python agent/testing/pdf_lines.py 原文.pdf 页码 [从第几磅 到第几磅]

按从上到下、从左到右排，每行：上沿 下沿 左 右（磅）、字号、文字（最多 60 个字）。
给了上下范围时只列这一段（PDF 坐标，从页顶往下量）。
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    args = sys.argv[1:]
    if len(args) not in (2, 4):
        print(__doc__)
        raise SystemExit(2)
    import pymupdf

    page = pymupdf.open(str(Path(args[0])))[int(args[1]) - 1]
    top, bottom = (float(args[2]), float(args[3])) if len(args) == 4 else (0.0, page.rect.height)
    rows = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [span for span in line["spans"] if span["text"].strip()]
            if not spans:
                continue
            x0, y0, x1, y1 = line["bbox"]
            if y1 < top or y0 > bottom:
                continue
            size = max(span["size"] for span in spans)
            rows.append((round(y0, 1), round(y1, 1), round(x0, 1), round(x1, 1), round(size, 1),
                         "".join(span["text"] for span in line["spans"])[:60]))
    for y0, y1, x0, x1, size, text in sorted(rows):
        print(f"{y0:7} {y1:7} {x0:7} {x1:7}  {size:4}  {text}")


if __name__ == "__main__":
    main()
