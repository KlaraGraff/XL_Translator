"""Shared geometry and IO helpers for the vector-preserving PDF shadow pipeline.

Coordinate spaces used throughout the pipeline:

* display px  -- pixels of the page rendered as the viewer sees it (page
  rotation applied) at ``dpi``.  OCR runs here.
* display pt  -- the same space in PDF points (px * 72 / dpi).
* page pt     -- PyMuPDF's unrotated page space; every drawing/redaction/text
  insertion call takes this space.  ``display_pt * page.derotation_matrix``.

Text direction is stored as the *display* rotation, counter-clockwise degrees
in which the text advances on screen: 0 = left->right, 90 = bottom->top,
180 = right->left (upside down), 270 = top->bottom.  ``insert_rotate`` turns
that into the ``rotate=`` argument of ``page.insert_text`` for any page
rotation (verified by tests/test_geometry.py on all 16 combinations).
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

from core.pdf_component import lazy_fitz

# PyMuPDF 来自「PDF 处理组件」，第一次用到 fitz.xxx 时才加载（见 core.pdf_component）；
# 没装组件时本包照样能 import，只是原位替换用不了。
fitz = lazy_fitz()

# unit advance vector and glyph-up vector in display space (y grows downward)
_ADV = {0: (1.0, 0.0), 90: (0.0, -1.0), 180: (-1.0, 0.0), 270: (0.0, 1.0)}
_UP = {0: (0.0, -1.0), 90: (-1.0, 0.0), 180: (0.0, 1.0), 270: (1.0, 0.0)}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s or "")).replace("’", "'")
    return re.sub(r"\s+", " ", s).strip()


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def px_to_display_pt(box, dpi: float) -> fitz.Rect:
    k = 72.0 / dpi
    return fitz.Rect(*(float(v) * k for v in box))


def display_pt_to_page(rect: fitz.Rect, page: fitz.Page) -> fitz.Rect:
    return fitz.Rect(rect) * page.derotation_matrix


def page_to_display_pt(rect: fitz.Rect, page: fitz.Page) -> fitz.Rect:
    return fitz.Rect(rect) * page.rotation_matrix


def display_point_to_page(pt: fitz.Point, page: fitz.Page) -> fitz.Point:
    return fitz.Point(pt) * page.derotation_matrix


def insert_rotate(display_rotation: int, page_rotation: int) -> int:
    return (int(display_rotation) + int(page_rotation)) % 360


def adv_up(display_rotation: int):
    r = int(display_rotation) % 360
    return _ADV[r], _UP[r]


def along_cross(rect: fitz.Rect, display_rotation: int):
    """Length along the reading direction and thickness across it."""
    if int(display_rotation) % 180 == 0:
        return rect.width, rect.height
    return rect.height, rect.width


def text_has_cjk(s: str) -> bool:
    return bool(re.search(r"[㐀-鿿豈-﫿]", s or ""))


NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def numbers(s: str) -> list[str]:
    """Numbers in ``s``; a decimal comma counts as a decimal point (French 1,5 = 1.5)."""
    return [n.replace(",", ".") for n in NUMBER_RE.findall(unicodedata.normalize("NFKC", str(s or "")))]
