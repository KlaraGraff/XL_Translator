"""列出 DXF 里单行文字（TEXT/ATTRIB/ATTDEF）的位置数据，用来和 AutoCAD 实际画出来的宽度对比。

云端看不到 AutoCAD 的真实字宽；对齐文字（右对齐、居中）的起点（10/20）是按估算宽度
重算的（core.cad_layout.sync_start_points）。这个脚本按同一套估算列出每段文字的
起点、对齐点、估算宽度，配合 cad_text_extents.lsp 在 AutoCAD 里量出的真实宽度，
就能看出哪一段估得不准、差多少。

用法（仓库根目录）::

    ./.venv/bin/python3 agent/testing/cad_text_report.py 译图.dxf [关键词 ...] > report.tsv

不给关键词时列出全部单行文字。输出制表符分隔，一段文字一行。
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cad_layout import (  # noqa: E402
    _float,
    _int,
    _single_line_fields,
    _style_fonts,
    estimate_text_width,
)
from core.cad_translation import _parse_dxf, _plain_text, _read_dxf_text  # noqa: E402

COLUMNS = (
    "handle", "type", "owner", "paper", "style", "font", "h72", "v", "x10", "y10", "x11", "y11",
    "height", "wfactor", "angle", "est_width", "start_gap", "text",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dxf", type=Path)
    parser.add_argument("needles", nargs="*", help="只列文字里含这些词的（任一）")
    args = parser.parse_args()
    records = _parse_dxf(_read_dxf_text(args.dxf))
    fonts = _style_fonts(records)
    print("\t".join(COLUMNS))
    for _start, _end, entity_type, fields in _single_line_fields(records):
        text = _plain_text(str(fields.get(1, "")))
        if args.needles and not any(needle in text for needle in args.needles):
            continue
        style = str(fields.get(7, "STANDARD")).strip().upper()
        font = fonts.get(style, "shx")
        height = _float(fields.get(40))
        factor = _float(fields.get(41), 1.0) or 1.0
        width = estimate_text_width(text, height, factor, font)
        angle = math.radians(_float(fields.get(50)))
        halign = _int(fields.get(72))
        # 起点到对齐点沿文字方向的距离，减去按估算宽度应有的距离：0 表示起点和估算一致。
        gap = ""
        if 11 in fields and halign in {1, 2, 4}:
            along = (_float(fields.get(11)) - _float(fields.get(10))) * math.cos(angle) + (
                _float(fields.get(21)) - _float(fields.get(20))
            ) * math.sin(angle)
            gap = f"{along - (width / 2 if halign in {1, 4} else width):.4f}"
        valign = fields.get(74) if entity_type != "TEXT" else fields.get(73)
        row = (
            fields.get(5, ""), entity_type, fields.get(330, ""), fields.get(67, "0"), style, font,
            halign, _int(valign), fields.get(10, ""), fields.get(20, ""), fields.get(11, ""), fields.get(21, ""),
            fields.get(40, ""), fields.get(41, ""), fields.get(50, ""), f"{width:.4f}", gap, text,
        )
        print("\t".join(str(value).strip() for value in row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
