"""量一量译图里的页面内容有没有被模型「画小」。

有的图像模型（实测 apiclub 上的 gpt-image-2.6-flare）交回来的图尺寸没问题，但把整页
内容缩到原来的四成左右，居中放在一大片白底上。比例检查拦不住它（画布比例是对的），
审核模型也常常放过它（字都在、也都译了），用户拿到的却是一页页小得没法读的 PDF。

这里分别量出原页和译图里「有内容的区域」占整页的宽、高比例，再拿译图除以原页：
1.0 是一样大，0.43 就是缩到了四成多。只用来判断，不去裁白边再放大——那样会把模型
画糊的小字放大成更糊的大字，还可能裁掉页眉页脚。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image

# 译图内容的宽和高都缩到原页的这个比例以下，才算「画小了」。翻译会改变文字长短，
# 一行变短、一段变窄都很正常，但页眉、页脚、表格边框、页边距这些都还在原位，
# 内容区域整体缩到八成以下，正常的翻译做不到。
CONTENT_SHRUNK_THRESHOLD = 0.80
# 原页内容区域在两个方向上都至少要占整页这么多，才拿来比：只有一个居中标题的封面页，
# 译文长一点短一点，宽度比例就能差出一大截，量了也不作数。
MIN_SOURCE_CONTENT_SPAN = 0.30
# 两头各忽略这么多「墨迹」，免得一个扫描噪点、一道装订阴影把内容区域撑大。
EDGE_TRIM_RATIO = 0.005
SAMPLE_LONG_EDGE_PX = 512
INK_LUMA_DELTA = 24


def content_span(image: Image.Image) -> tuple[float, float] | None:
    """有内容的区域占整页的（宽比例, 高比例）；整页几乎空白时返回 None。"""
    gray = image.convert("L")
    gray.thumbnail((SAMPLE_LONG_EDGE_PX, SAMPLE_LONG_EDGE_PX), Image.Resampling.BILINEAR)
    width, height = gray.size
    if width <= 0 or height <= 0:
        return None
    histogram = gray.histogram()[:256]
    background = histogram.index(max(histogram))
    lookup = [255 if abs(level - background) > INK_LUMA_DELTA else 0 for level in range(256)]
    mask = gray.point(lookup)
    data = mask.tobytes()
    columns = [0] * width
    rows = [0] * height
    for y in range(height):
        row = data[y * width : (y + 1) * width]
        count = row.count(255)
        if not count:
            continue
        rows[y] = count
        for x, value in enumerate(row):
            if value:
                columns[x] += 1
    total = sum(rows)
    if total < max(20, width * height * 0.001):
        return None
    left, right = _trimmed_extent(columns, total)
    top, bottom = _trimmed_extent(rows, total)
    return (right - left + 1) / width, (bottom - top + 1) / height


def _trimmed_extent(counts: list[int], total: int) -> tuple[int, int]:
    trim = total * EDGE_TRIM_RATIO
    start, seen = 0, 0
    for index, count in enumerate(counts):
        seen += count
        if seen > trim:
            start = index
            break
    end, seen = len(counts) - 1, 0
    for index in range(len(counts) - 1, -1, -1):
        seen += counts[index]
        if seen > trim:
            end = index
            break
    return start, max(start, end)


def content_scale(
    image_bytes: bytes,
    source_image_path: Path | str | None,
) -> float | None:
    """译图内容相对原页内容的大小（取宽、高两个方向里大的那个）；量不准时返回 None。

    取大的那个是为了少误判：模型整页缩小时宽高一起缩，两个都小；
    正常翻译最多让某一个方向变窄一点。
    """
    if not source_image_path:
        return None
    try:
        with Image.open(Path(source_image_path)) as source:
            source.load()
            source_span = content_span(source)
        with Image.open(BytesIO(image_bytes)) as output:
            output.load()
            output_span = content_span(output)
    except Exception:  # noqa: BLE001 - 量不出来就当「不确定」，不拦页面。
        return None
    if source_span is None or output_span is None:
        return None
    if min(source_span) < MIN_SOURCE_CONTENT_SPAN:
        return None
    return max(output_span[0] / source_span[0], output_span[1] / source_span[1])


def is_content_shrunk(scale: float | None) -> bool:
    return scale is not None and scale < CONTENT_SHRUNK_THRESHOLD
