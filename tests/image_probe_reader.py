"""测试用：像一个真能看图的模型那样，读出「能不能识别图片」测试图里的码。

假的模型服务（tests 里的各个 _Fake* 服务器）拿到请求里的 data URL，交给这里读；
读法照着 core/image_input_check.draw_probe_image 的画法反过来取点。
"""

from __future__ import annotations

import base64
from io import BytesIO
from typing import Any

from PIL import Image

from core.image_input_check import (
    PROBE_CELL_PX,
    PROBE_GLYPH_GAP,
    PROBE_GLYPHS,
    PROBE_MARGIN,
)

_BY_SHAPE = {shape: char for char, shape in PROBE_GLYPHS.items()}


def read_probe_png(png: bytes) -> str:
    """读出测试图里的码；不是测试图（比如整页图）时返回 ""。"""
    try:
        image = Image.open(BytesIO(png)).convert("L")
    except Exception:  # noqa: BLE001 - not an image we drew
        return ""
    cell = PROBE_CELL_PX
    columns, rows = image.width // cell, image.height // cell
    if rows != PROBE_MARGIN * 2 + 7 or image.width % cell or image.height % cell:
        return ""
    count = (columns - PROBE_MARGIN * 2 + PROBE_GLYPH_GAP) // (5 + PROBE_GLYPH_GAP)
    code = ""
    for index in range(count):
        left = PROBE_MARGIN + index * (5 + PROBE_GLYPH_GAP)
        shape = tuple(
            "".join(
                "#" if image.getpixel(((left + col) * cell + cell // 2, (PROBE_MARGIN + row) * cell + cell // 2)) < 128 else "."
                for col in range(5)
            )
            for row in range(7)
        )
        char = _BY_SHAPE.get(shape)
        if char is None:
            return ""
        code += char
    return code


def read_probe_data_url(url: str) -> str:
    if not str(url or "").startswith("data:image/"):
        return ""
    try:
        return read_probe_png(base64.b64decode(url.split(",", 1)[1]))
    except Exception:  # noqa: BLE001
        return ""


def probe_code_in_request(payload: Any) -> str:
    """一个 chat / responses 请求体里如果带着测试图，读出它的码。"""
    found = ""

    def walk(value: Any) -> None:
        nonlocal found
        if found:
            return
        if isinstance(value, dict):
            url = value.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            if isinstance(url, str):
                found = read_probe_data_url(url)
                if found:
                    return
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return found
