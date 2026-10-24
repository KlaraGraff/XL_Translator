"""CAD 译图版面体检：译文有没有压到别的文字、穿过格线（表格、图签、图框、墙）。

云端没有 AutoCAD，看不到译图的真实样子；这个脚本按应用里同一套字宽（simplex/romans
按逐字宽度，与 AutoCAD 实测相差几个百分点）估出每段文字的外框，和原图对比，只报
**译图里新出现的**问题：

- 压字：一段新文字（译文、双语新增行）的外框和另一段文字的外框重叠，原图里这两处
  （按插入点认）并不重叠；
- 穿线：一段新文字的外框被一条实线横穿、竖穿或斜穿（含门的开启弧、箭头这类弧线和斜线），
  原图同一位置的文字并没有穿过这条线。

用法（仓库根目录）::

    ./.venv/bin/python3 agent/testing/cad_layout_check.py 原图.dxf 译图.dxf [--png 预览.png] [--window x1,y1,x2,y2]

退出码 0 没有新问题，1 有。``--png`` 另画一张示意图：灰色是线，绿色框是新文字，红色是
有问题的；装了 Hershey-Fonts（``pip install Hershey-Fonts``，只用于自测）时 simplex
文字按真实笔画画出来，否则只画外框。
"""
from __future__ import annotations

import argparse
import math
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cad_layout import (  # noqa: E402
    _box,
    _collect_entities,
    _geometry,
    _horizontal_span,
    _local,
    _mtext_extent,
    _mtext_paragraphs,
    _mtext_raw,
    _TextEntity,
    _to_local,
    _vertical_span,
    mtext_narrowing,
)
from core.cad_translation import _parse_dxf, _plain_text  # noqa: E402


@dataclass(frozen=True)
class _Text:
    entity: _TextEntity
    key: tuple
    left: float
    right: float
    bottom: float
    top: float


def _load(path: Path) -> tuple[list[_Text], dict]:
    raw = path.read_bytes()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("gb18030", errors="replace")
    records = _parse_dxf(content)
    texts = []
    for entity in _collect_entities(records, _plain_text):
        box = _box(entity)
        x, y = _local(entity)
        left, right, bottom, top = box.left, box.right, box.bottom, box.top
        if entity.entity_type == "MTEXT":
            # 多行文字按宽度（组码 41）自动换行：译文折成几行就有几行高。
            raw = _mtext_raw(records, entity.start, entity.end)
            width, height = _mtext_extent(
                _mtext_paragraphs(raw, entity.height), entity.height, entity.ref_width, entity.font_class,
                mtext_narrowing(raw),
            )
            (left, right), (bottom, top) = (_horizontal_span(entity.halign, x, width),
                                            _vertical_span(entity.valign, y, height))
        key = (entity.container, round(entity.rotation, 1), round(x, 1), round(y, 1))
        texts.append(_Text(entity, key, left, right, bottom, top))
    lines, curves = _geometry(records)
    return texts, {key: lines.get(key, []) + curves.get(key, []) for key in set(lines) | set(curves)}


def _overlap(a: _Text, b: _Text) -> bool:
    if a.entity.container != b.entity.container or round(a.entity.rotation, 1) != round(b.entity.rotation, 1):
        return False
    height = min(a.top - a.bottom, b.top - b.bottom)
    vertical = min(a.top, b.top) - max(a.bottom, b.bottom)
    horizontal = min(a.right, b.right) - max(a.left, b.left)
    return vertical > 0.25 * height and horizontal > 0.25 * height


def _crossed(text: _Text, segment: tuple[float, float, float, float]) -> bool:
    (ax, ay), (bx, by) = (_to_local(segment[0], segment[1], text.entity.rotation),
                          _to_local(segment[2], segment[3], text.entity.rotation))
    height = text.top - text.bottom
    length = math.hypot(bx - ax, by - ay)
    if abs(ax - bx) <= 1e-6 * length:
        # 竖线：在字的左右两端之间，并且盖过字高的中间一段。
        return (text.left + 0.1 * height < ax < text.right - 0.1 * height
                and min(ay, by) < text.bottom + 0.3 * height and max(ay, by) > text.top - 0.3 * height)
    if abs(ay - by) <= 1e-6 * length:
        # 横线：从字身中间穿过（贴着字底、字顶的不算）。
        return (text.bottom + 0.15 * height < ay < text.top - 0.15 * height
                and min(ax, bx) < text.right - 0.5 * height and max(ax, bx) > text.left + 0.5 * height)
    # 斜线、弧线折成的弦：和字身中间那块（左右各让 0.1 字高，上下各让 0.15）有交点就算。
    low, high = 0.0, 1.0
    for delta, start, lo, hi in ((bx - ax, ax, text.left + 0.1 * height, text.right - 0.1 * height),
                                 (by - ay, ay, text.bottom + 0.15 * height, text.top - 0.15 * height)):
        if abs(delta) < 1e-12:
            if not lo < start < hi:
                return False
            continue
        t1, t2 = sorted(((lo - start) / delta, (hi - start) / delta))
        low, high = max(low, t1), min(high, t2)
    return low < high


def check(source: Path, translated: Path) -> tuple[list[str], list[_Text], list[_Text], list[_Text], dict]:
    source_texts, source_segments = _load(source)
    texts, segments = _load(translated)
    source_by_key = {text.key: text for text in source_texts}
    unchanged = {
        text.key for text in texts
        if text.key in source_by_key and source_by_key[text.key].entity.text == text.entity.text
    }
    new = [text for text in texts if text.key not in unchanged]
    issues: list[str] = []
    flagged: list[_Text] = []
    for text in new:
        problems = []
        for other in texts:
            if other is text or not _overlap(text, other):
                continue
            before_a, before_b = source_by_key.get(text.key), source_by_key.get(other.key)
            if before_a and before_b and _overlap(before_a, before_b):
                continue
            if other in new and id(other) < id(text):
                # 两段都是新的：只报一次。
                continue
            problems.append(f"压到「{other.entity.text[:30]}」")
        before = source_by_key.get(text.key)
        for segment in segments.get(text.entity.container, ()):
            if _crossed(text, segment) and not (before and _crossed(before, segment)):
                x1, y1, x2, y2 = segment
                problems.append(f"穿过线 ({x1:g},{y1:g})-({x2:g},{y2:g})")
                break
        if problems:
            flagged.append(text)
            issues.append(f"「{text.entity.text[:50]}」@({text.entity.anchor_x:g},{text.entity.anchor_y:g})：" + "；".join(problems))
    return issues, texts, new, flagged, segments


def render(path: Path, texts: list[_Text], new: list[_Text], flagged: list[_Text], segments: dict,
           window: tuple[float, float, float, float] | None, width: int = 2400) -> None:
    from PIL import Image, ImageDraw

    try:
        from HersheyFonts import HersheyFonts
        hershey = HersheyFonts()
        hershey.load_default_font("futural")
    except ImportError:
        hershey = None
    model = ("SPACE", "0")
    lines = segments.get(model, [])
    if window is None:
        xs = [v for s in lines for v in (s[0], s[2])] + [t.left for t in texts] + [t.right for t in texts]
        ys = [v for s in lines for v in (s[1], s[3])] + [t.bottom for t in texts] + [t.top for t in texts]
        window = (min(xs), min(ys), max(xs), max(ys))
    x1, y1, x2, y2 = window
    scale = width / (x2 - x1)
    image = Image.new("RGB", (width, int((y2 - y1) * scale) + 1), (34, 40, 49))
    draw = ImageDraw.Draw(image)

    def point(x: float, y: float) -> tuple[float, float]:
        return (x - x1) * scale, (y2 - y) * scale

    for sx1, sy1, sx2, sy2 in lines:
        draw.line([point(sx1, sy1), point(sx2, sy2)], fill=(150, 150, 150), width=1)
    new_ids, flagged_ids = {id(t) for t in new}, {id(t) for t in flagged}
    for text in texts:
        if text.entity.container != model or text.entity.rotation:
            continue
        color = (235, 90, 80) if id(text) in flagged_ids else (120, 210, 120) if id(text) in new_ids else (200, 200, 200)
        entity = text.entity
        # 重音字母按底字母画（é → e），宽度与应用里的估算一致。
        strokes_text = "".join(c for c in unicodedata.normalize("NFD", entity.text) if not unicodedata.combining(c))
        if hershey is not None and entity.font_class == "hershey" and strokes_text.isascii():
            unit = entity.height / 21.0
            cursor = text.left
            for glyph in hershey.glyphs_for_text(strokes_text):
                for stroke in glyph.strokes:
                    points = [point(cursor + (gx - glyph.left_offset) * unit * entity.width_factor,
                                    text.bottom + (9 - gy) * unit) for gx, gy in stroke]
                    if len(points) > 1:
                        draw.line(points, fill=color, width=1)
                cursor += glyph.char_width * unit * entity.width_factor
        else:
            draw.rectangle([point(text.left, text.top), point(text.right, text.bottom)], outline=color, width=1)
    image.save(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("translated", type=Path)
    parser.add_argument("--png", type=Path)
    parser.add_argument("--window", help="x1,y1,x2,y2（图纸坐标）")
    args = parser.parse_args()
    issues, texts, new, flagged, segments = check(args.source, args.translated)
    for line in issues:
        print(line)
    print(f"新文字 {len(new)} 段，有问题 {len(issues)} 段")
    if args.png:
        window = tuple(float(v) for v in args.window.split(",")) if args.window else None
        render(args.png, texts, new, flagged, segments, window)
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
