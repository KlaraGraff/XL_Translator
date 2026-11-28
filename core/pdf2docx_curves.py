"""PDF 转 Word 时，圆、椭圆这类曲线图形按图片保留，不再被 pdf2docx 当成长方形丢掉。

pdf2docx 把页面上画出来的图形分两种：横平竖直的长方形（框线、底色）拿去拼表格和文字底色，
其余的（斜线、曲线）连同周围一片按原页面截成图片放进 Word，图里的字另外留成文字。分辨的
办法是看图形占它外接矩形的面积：到九成就算长方形。可它算曲线的面积时用的是贝塞尔曲线的
控制点，不是曲线本身：四段曲线画成的圆，控制点围出来的面积正好是外接正方形的九成
（真实的圆只有 π/4 ≈ 七成八），于是圆被当成长方形。长方形只能变成表格线和底色，圆的边线
就没了、白色填充的圆盖不住底色：讲义第 48 页图 13 的维恩图只剩灰底和 A、B 两个字母；
第 57 页箭头图里没有箭头连着的点（没被映射到的元素）也丢了。

做法：只改这一条判断，曲线按曲线本身量面积（每段贝塞尔曲线取若干点连成折线），其余照旧。
圆、椭圆于是和别的曲线图形一样，由 pdf2docx 自己连同周围截成图片，标签仍是可翻译的文字。
圆角框（标题条、Note 框）真实面积仍在九成以上，照旧当长方形，转成带底色的表格。

只用于一键安装的组件（固定的 pdf2docx 版本，在本程序的子进程里转换，见
``core.pdf_component._convert_in_process``）；用户自己装的 pdf2docx 走它自己的命令行，改不到。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

#: 每段贝塞尔曲线取这么多个点连成折线来量面积：圆量出来约 0.78（真值 π/4），离九成很远。
_CURVE_STEPS = 8


def _curve_points(p0, p1, p2, p3) -> list[tuple[float, float]]:
    """曲线上（不含起点）均匀取的点。"""
    points = []
    for step in range(1, _CURVE_STEPS + 1):
        t = step / _CURVE_STEPS
        a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3
        points.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                       a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return points


def outline(segments: Any, curve_class: type) -> list[tuple[float, float]]:
    """一条子路径的轮廓点：直线、矩形照原样，曲线换成曲线上的点。"""
    points: list[tuple[float, float]] = []
    for segment in segments:
        corners = [(float(point[0]), float(point[1])) for point in segment.points]
        if isinstance(segment, curve_class):
            if not points:
                points.append(corners[0])
            points.extend(_curve_points(*corners))
        else:
            points.extend(corners)
    return points


def fills_its_box(points: list[tuple[float, float]], share: float) -> bool:
    """和 pdf2docx 的判断一样（外接矩形面积为 0 的直线算；不闭合的不算；面积到 share 算），
    只是面积按传进来的轮廓点算。"""
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    box = (max(xs) - min(xs)) * (max(ys) - min(ys))
    if box == 0:
        return True
    (x0, y0), (x1, y1) = points[0], points[-1]
    if abs(x0 - x1) + abs(y0 - y1) > 1e-3:
        return False
    area = sum(xa * yb - xb * ya for (xa, ya), (xb, yb) in zip(points, points[1:]))
    return abs(area / 2) / box >= share


@contextmanager
def curves_measured_on_the_curve() -> Iterator[None]:
    """在这段时间里，pdf2docx 判断「是不是长方形」时按曲线本身量面积。"""
    from pdf2docx.common import constants
    from pdf2docx.shape import Path as path_module

    segments_class = path_module.Segments
    curve_class = path_module.C
    share = constants.FACTOR_MOST
    original = segments_class.__dict__["is_iso_oriented"]

    def is_iso_oriented(self) -> bool:
        return fills_its_box(outline(self, curve_class), share)

    setattr(segments_class, "is_iso_oriented", property(is_iso_oriented))
    try:
        yield
    finally:
        setattr(segments_class, "is_iso_oriented", original)
