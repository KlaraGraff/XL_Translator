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


# 译成西文时日期常写成「24 mars 2026」「24/03/2026」「24.03.2026」。核对数字时把这几种
# 写法拆成日、月、年三个数：紧挨着数字的月份名按月份的数字算（离数字远的同形词 mai、
# may 不算），03 与 3 算同一个数，「2026年3月24日」才对得上。
_MONTH_NAMES = (
    ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
     "novembre", "décembre"),
    ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"),
    ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
     "noviembre", "diciembre"),
    ("januar", "februar", "märz", "april", "mai", "juni", "juli", "august", "september", "oktober",
     "november", "dezember"),
    ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre",
     "novembre", "dicembre"),
    ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
     "novembro", "dezembro"),
)
MONTHS = {name: index for names in _MONTH_NAMES for index, name in enumerate(names, 1)}
MONTHS.update({"fevrier": 2, "aout": 8, "decembre": 12, "marz": 3, "marco": 3,
               "jan": 1, "janv": 1, "feb": 2, "fév": 2, "févr": 2, "fev": 2, "mar": 3, "apr": 4, "avr": 4,
               "jun": 6, "jul": 7, "juil": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11,
               "dec": 12, "déc": 12})
_MONTH_RE = re.compile(r"(?<![^\W\d_])(" + "|".join(sorted(map(re.escape, MONTHS), key=len, reverse=True))
                       + r")\.?(?![^\W\d_])", re.IGNORECASE)
_DIGIT_BEFORE_RE = re.compile(r"\d(?:er|st|nd|rd|th)?[\s.,/-]*$", re.IGNORECASE)
_DIGIT_AFTER_RE = re.compile(r"[\s.,/-]*\d")
_NUMERIC_DATE_RE = re.compile(r"(?<![\d.,])(\d{1,4})([./-])(\d{1,2})\2(\d{1,4})(?![\d.,])")


def _month_numbers(s: str) -> str:
    def swap(m: re.Match[str]) -> str:
        near = _DIGIT_BEFORE_RE.search(s, 0, m.start()) or _DIGIT_AFTER_RE.match(s, m.end())
        return f" {MONTHS[m.group(1).lower()]} " if near else m.group(0)

    return _MONTH_RE.sub(swap, s)


def numbers(s: str, *, dates: bool = False) -> list[str]:
    """Numbers in ``s``; a decimal comma counts as a decimal point (French 1,5 = 1.5).

    ``dates``: dates count as day, month and year whatever way they are written,
    so 「2026年3月24日」, 「24 mars 2026」 and 「24/03/2026」 hold the same numbers."""
    text = unicodedata.normalize("NFKC", str(s or ""))
    if not dates:
        return [n.replace(",", ".") for n in NUMBER_RE.findall(text)]
    text = _NUMERIC_DATE_RE.sub(r"\1 \3 \4", _month_numbers(text))
    return [n.replace(",", ".") if any(c in n for c in ".,") else (n.lstrip("0") or "0")
            for n in NUMBER_RE.findall(text)]


def join_words(words: list[dict]) -> str:
    """Words of one line back into text.  Tesseract's words are separated by spaces;
    PP-OCR's carry ``sp`` (Chinese and Japanese characters are words of their own,
    written without spaces)."""
    out = ""
    for w in words:
        if out and w.get("sp", True):
            out += " "
        out += w["text"]
    return out
