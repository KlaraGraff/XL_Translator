"""生成「规则没通过改由模型复查」（PR #258）的本机实测样张：全是编的公开内容。

用法::

    ./.venv/bin/python3 agent/testing/rule_review_samples.py <输出文件夹>

生成 rule_review.xlsx、rule_review.dxf、rule_review.txt 三份，内容相同：每一行都是规则容易
误判、但正确译文写法很常见的情况（小数逗号、数字换位置、数字写成单词、日期换写法……），
以及几条规则本来就能通过的对照行。另写一份 expected.json：每条原文里必须在译文中找到的数。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# (原文, 译文里必须找得到的数；按目标语言写法可以是 2,5 / 2.5 / 1 500 等，人工核对时看「值」)
ROWS: list[tuple[str, list[str]]] = [
    ("25厚1:2.5水泥砂浆找平", ["25", "1", "2.5"]),  # 反馈 #23：法文常写 1:2,5，顺序也会换
    ("坡度1:2.5，坡向排水沟", ["1", "2.5"]),
    ("层高3.6米，共12层", ["3.6", "12"]),
    ("共3层", ["3"]),  # 模型常写成 trois / three
    ("2026年3月24日开工", ["2026", "3", "24"]),  # 写成 24 mars 2026
    ("合同总价1,500元", ["1500"]),  # 写成 1 500
    ("预付款为合同价的30%", ["30"]),
    ("混凝土强度等级C30，保护层厚度25mm", ["30", "25"]),
    ("设备基坑：2830X1700mm", ["2830", "1700"]),
    ("第十二条 付款方式", []),
    ("钢筋间距@200，直径12", ["200", "12"]),
    ("室内外高差0.45米", ["0.45"]),
    ("门", []),  # 对照：规则直接通过
    ("消防通道", []),  # 对照
]


def main(out_dir: str) -> None:
    out = Path(out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "清单"
    for row, (text, _numbers) in enumerate(ROWS, start=1):
        ws.cell(row=row, column=1, value=text)
    wb.save(out / "rule_review.xlsx")

    (out / "rule_review.txt").write_text("\n".join(text for text, _ in ROWS) + "\n", encoding="utf-8")

    lines = ["0", "SECTION", "2", "ENTITIES"]
    for index, (text, _numbers) in enumerate(ROWS):
        lines += ["0", "TEXT", "8", "0", "10", "0", "20", str(-index * 10), "30", "0", "40", "2.5", "1", text]
    lines += ["0", "ENDSEC", "0", "EOF"]
    (out / "rule_review.dxf").write_text("\n".join(lines) + "\n", encoding="utf-8")

    (out / "expected.json").write_text(
        json.dumps({text: numbers for text, numbers in ROWS}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已生成 {len(ROWS)} 条样张：{out}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
