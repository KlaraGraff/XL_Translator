"""测试用：像一个真能看图的模型那样，读出「能不能识别图片」测试图里的码。

假的模型服务（tests 里的各个 _Fake* 服务器）拿到请求里的 data URL，交给这里读；
读法照着 core/image_input_check.draw_probe_image 的画法逐格比对。
"""

from __future__ import annotations

import base64
from io import BytesIO
from typing import Any

from PIL import Image, ImageChops

from core.image_input_check import (
    PROBE_CELL_SIZE,
    PROBE_CODE_ALPHABET,
    PROBE_GAP_PX,
    PROBE_MARGIN_PX,
    probe_cell_origins,
    probe_glyph,
)

_GLYPHS = {char: probe_glyph(char) for char in PROBE_CODE_ALPHABET}


def read_probe_png(png: bytes) -> str:
    """读出测试图里的码；不是测试图（比如整页图）时返回 ""。

    每个字画在固定的格子里，逐格和字母表里的每个字的画法比对，一模一样才算。
    """
    try:
        image = Image.open(BytesIO(png)).convert("L")
    except Exception:  # noqa: BLE001 - not an image we drew
        return ""
    width, height = PROBE_CELL_SIZE
    if image.height != PROBE_MARGIN_PX * 2 + height:
        return ""
    count, rest = divmod(image.width - PROBE_MARGIN_PX * 2 + PROBE_GAP_PX, width + PROBE_GAP_PX)
    if rest or count < 1:
        return ""
    code = ""
    for left, top in probe_cell_origins(count):
        cell = image.crop((left, top, left + width, top + height))
        char = next(
            (char for char, glyph in _GLYPHS.items() if ImageChops.difference(cell, glyph).getbbox() is None),
            None,
        )
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
