"""Vector-preserving writeback of translated rows into a single-page PDF.

Old text is removed, never painted over:

* native text rows -> text redaction (``text=REMOVE``, graphics/images kept);
* OCR rows (vector outline text) -> ``graphics=REMOVE_IF_COVERED``: only
  paths that lie *completely* inside the row's rectangle are deleted.  Glyph
  outlines qualify; leader lines, table rules, frames and underlines extend
  past the rectangle and survive untouched, so nothing has to be redrawn.

A trial redaction is rendered first.  Rows whose ink does not disappear
(text inside a raster image, glyphs merged with a long path) keep their
original text and are reported as ``source_not_removable``.

The translation size is derived from the source glyph height, not squeezed
into the tight OCR rectangle.  Lines may extend into verified blank space; when
that space runs out they shrink by the same tiers as CAD (core.cad_layout:
first narrower, from 70 % also smaller, down to the user's minimum), and only
then wrap into verified blank lines below.  Lines of a paragraph always flow
through its own source line slots.  Colour is sampled from the source ink.

Each line is written in a system font that has every one of its glyphs
(core/pdf_vector/fonts.py); a line no font can show is left as the source.
"""
from __future__ import annotations

import argparse
import math
import re
import statistics
from pathlib import Path

from PIL import Image

from core.cad_layout import DEFAULT_FIT_SETTINGS_ZH, FitSettings, scale_parts

from .common import (
    fitz,
    adv_up, display_point_to_page, display_pt_to_page, insert_rotate,
    px_to_display_pt, read_json, sha256, write_json,
)
from .fonts import FontSet, has_cjk, looks_bold, looks_serif, needs_shaping

Image.MAX_IMAGE_PIXELS = None
FONT_NAME = "CNF"
INK_GRAY = 200            # gray level counted as ink
RESIDUAL_LIMIT = 0.35     # share of source ink allowed to survive redaction
MIN_FONT_PT = 1.6
# up to this many verified blank lines below a paragraph take the wrapped rest; a
# one-line label gets at most LABEL_EXTRA_LINES (Chinese labels often come back
# six or eight times as long in French, so three lines alone would force 50 %)
MAX_EXTRA_LINES = 8
LABEL_EXTRA_LINES = 3


# ----------------------------------------------------------------- raster

def render(page: fitz.Page, dpi: float) -> Image.Image:
    pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def clamp_box(box, im):
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    return max(0, x0), max(0, y0), min(im.width, x1), min(im.height, y1)


def ink_count(gray: Image.Image, box, light: bool = False) -> tuple[int, int]:
    """Ink pixels in the box. light=True counts bright pixels instead: white text
    on a dark band (table headers, title bars) is bright ink on dark ground."""
    b = clamp_box(box, gray)
    if b[2] <= b[0] or b[3] <= b[1]:
        return 0, 0
    hist = gray.crop(b).histogram()
    ink = sum(hist[256 - (256 - INK_GRAY) // 2:]) if light else sum(hist[:INK_GRAY])
    return ink, (b[2] - b[0]) * (b[3] - b[1])


def dark_ground(gray: Image.Image, box) -> bool:
    b = clamp_box(box, gray)
    if b[2] <= b[0] or b[3] <= b[1]:
        return False
    hist = gray.crop(b).histogram()
    total = sum(hist)
    return sum(hist[:128]) > total * 0.5


def is_light(color) -> bool:
    r, g, b = (list(color or (0, 0, 0)) + [0, 0, 0])[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b > 0.7


def sample_color(rgb: Image.Image, box) -> tuple[float, float, float]:
    """Dominant ink colour inside the box (mode of quantised ink pixels)."""
    b = clamp_box(box, rgb)
    if b[2] <= b[0] or b[3] <= b[1]:
        return (0.0, 0.0, 0.0)
    crop = rgb.crop(b)
    px = list(crop.get_flattened_data() if hasattr(crop, "get_flattened_data") else crop.getdata())
    ink = [p for p in px if (765 - sum(p)) > 150 or (max(p) - min(p)) > 90]
    if not ink:
        return (0.0, 0.0, 0.0)
    buckets: dict = {}
    for p in ink:
        buckets.setdefault((p[0] // 48, p[1] // 48, p[2] // 48), []).append(p)
    # strongest bucket; ties go to the more saturated/darker (anti-aliased edges are pale)
    top = max(buckets.values(), key=lambda v: (len(v), -sum(v[0])))
    c = tuple(sorted(q[i] for q in top)[len(top) // 4] / 255.0 for i in range(3))
    if max(c) - min(c) < 0.1 and max(c) < 0.5:
        return (0.0, 0.0, 0.0)
    return c


# ----------------------------------------------------------------- text

# Han, kana and CJK punctuation break between any two characters; every other run
# (Latin, Cyrillic, Greek or Hangul words, numbers, codes) stays whole.
_CJK_CHARS = "\u3000-\u30ff\u3400-\u9fff\uf900-\ufaff\uff00-\uffef"
TOKEN_RE = re.compile(rf"[{_CJK_CHARS}]|[^\s{_CJK_CHARS}]+|\s+", re.S)


def wrap(text: str, font: fitz.Font, fs: float, widths: list[float], squeeze: float = 1.0) -> list[str] | None:
    """Greedy wrap into len(widths) lines; words, numbers and codes never split.
    ``squeeze`` narrows the glyphs (width factor), as written by place_line."""
    toks = TOKEN_RE.findall(text)
    lines, cur, i = [], "", 0
    for t in toks:
        w_lim = widths[min(i, len(widths) - 1)]
        cand = cur + t
        if font.text_length(cand.rstrip(), fontsize=fs) * squeeze <= w_lim or not cur.strip():
            if font.text_length(cand.rstrip(), fontsize=fs) * squeeze > w_lim and not cur.strip():
                return None  # single token wider than the line
            cur = cand
            continue
        lines.append(cur.rstrip())
        i += 1
        if i >= len(widths):
            return None
        cur = t.lstrip()
    if cur.strip():
        lines.append(cur.rstrip())
    return lines if len(lines) <= len(widths) else None


def em_from_box(src: str, thick_pt: float) -> float:
    """Estimate the source font em size from an OCR line thickness."""
    letters = [c for c in src if c.isalpha()]
    if not letters:
        return thick_pt / 0.72
    has_desc = any(c in "gjpqyQ,;()[]" for c in src)
    has_lower_asc = any(c in "bdfhklt" for c in src) or any(c.isupper() for c in src)
    all_caps = all(not c.islower() for c in letters)
    if has_desc and not all_caps:
        return thick_pt / 0.95
    if all_caps or has_lower_asc:
        return thick_pt / 0.74
    return thick_pt / 0.55  # x-height only


# ----------------------------------------------------------------- layout

def content_box(gray: Image.Image, pad_px: float = 0.0, rows=()) -> tuple[float, float, float, float] | None:
    """Outermost ink of the source page (px), and of the source line boxes ``rows``
    (a text line's box reaches above its ink: the font's ascender). Translations
    never grow past it: a paragraph on a text page would otherwise run into the
    page margin."""
    mask = gray.point(lambda v: 255 if v < 200 else 0)
    box = mask.getbbox()
    if box is None:
        return None
    x0, y0, x1, y1 = box
    for b in rows:
        x0, y0, x1, y1 = min(x0, b[0]), min(y0, b[1]), max(x1, b[2]), max(y1, b[3])
    return (x0 - pad_px, y0 - pad_px, x1 + pad_px, y1 + pad_px)


class Layout:
    def __init__(self, gray_after: Image.Image, dpi: float, bounds=None):
        self.gray = gray_after
        self.dpi = dpi
        self.k = dpi / 72.0
        self.occupied: list[tuple[float, float, float, float]] = []
        self.bounds = bounds

    def blank(self, box) -> bool:
        if self.bounds is not None:
            bx0, by0, bx1, by1 = self.bounds
            if box[0] < bx0 or box[1] < by0 or box[2] > bx1 or box[3] > by1:
                return False
        ink, area = ink_count(self.gray, box)
        if area == 0:
            return False
        if ink > max(2, area * 0.0015):
            return False
        x0, y0, x1, y1 = box
        return not any(not (x1 <= a or x0 >= c or y1 <= b or y0 >= d) for a, b, c, d in self.occupied)

    def extend(self, box, rot: int, forward: bool, limit_px: float) -> float:
        """How far (px) the line band can grow past ``box`` in one direction."""
        (ax, ay), _ = adv_up(rot)
        sgn = 1 if forward else -1
        x0, y0, x1, y1 = box
        thick = (y1 - y0) if rot % 180 == 0 else (x1 - x0)
        step = max(2.0, thick * 0.25)
        grown = 0.0
        while grown + step <= limit_px:
            d0, d1 = grown, grown + step
            if rot % 180 == 0:
                dirx = ax * sgn
                sx0, sx1 = (x1 + d0, x1 + d1) if dirx > 0 else (x0 - d1, x0 - d0)
                probe = (sx0, y0, sx1, y1)
            else:
                diry = ay * sgn
                sy0, sy1 = (y1 + d0, y1 + d1) if diry > 0 else (y0 - d1, y0 - d0)
                probe = (x0, sy0, x1, sy1)
            if not self.blank(probe):
                break
            grown = d1
        return grown


def slot_geometry(box_px, rot):
    """Return (start_px_along, length_px, center_cross_px, thick_px) of a line."""
    x0, y0, x1, y1 = box_px
    if rot == 0:
        return x0, x1 - x0, (y0 + y1) / 2, y1 - y0
    if rot == 180:
        return x1, x1 - x0, (y0 + y1) / 2, y1 - y0
    if rot == 90:
        return y1, y1 - y0, (x0 + x1) / 2, x1 - x0
    return y0, y1 - y0, (x0 + x1) / 2, x1 - x0


def _metrics(text: str) -> tuple[float, float, float]:
    """(baseline below the line centre, ink above the centre, ink below), in em."""
    if has_cjk(text):
        # CJK ink spans roughly baseline-0.88em .. baseline+0.12em; centre it.
        return 0.38, 0.5, 0.5
    # Latin, Cyrillic, Greek: capitals and ascenders reach baseline-0.75em,
    # descenders baseline+0.22em
    return 0.33, 0.45, 0.55


def _baseline(text, fs, rot, start_along_px, center_cross_px, dpi):
    k = 72.0 / dpi
    _, (ux, uy) = adv_up(rot)
    shift = _metrics(text)[0] * fs
    if rot % 180 == 0:
        sx, sy = start_along_px * k, center_cross_px * k
    else:
        sx, sy = center_cross_px * k, start_along_px * k
    return sx, sy, sx - ux * shift, sy - uy * shift


def line_rect(font, text, fs, rot, start_along_px, center_cross_px, dpi, squeeze=1.0):
    """Display px rectangle a line will occupy (no drawing)."""
    k = 72.0 / dpi
    (ax, ay), (ux, uy) = adv_up(rot)
    sx, sy, bx, by = _baseline(text, fs, rot, start_along_px, center_cross_px, dpi)
    width_pt = font.text_length(text, fontsize=fs) * squeeze
    ex, ey = bx + ax * width_pt, by + ay * width_pt
    tx0, tx1 = sorted([bx, ex])
    ty0, ty1 = sorted([by, ey])
    _, above, below = _metrics(text)
    if rot % 180 == 0:
        lo, hi = (sy - above * fs, sy + below * fs) if uy < 0 else (sy - below * fs, sy + above * fs)
        return (tx0 / k, lo / k, tx1 / k, hi / k)
    lo, hi = (sx - above * fs, sx + below * fs) if ux < 0 else (sx - below * fs, sx + above * fs)
    return (lo / k, ty0 / k, hi / k, ty1 / k)


def place_line(page, font, text, fs, rot, start_along_px, center_cross_px, dpi, color, *,
               fontname=FONT_NAME, squeeze=1.0):
    """Insert one line; return its display px rectangle.  ``squeeze`` < 1 narrows
    the glyphs along the reading direction (CAD's width factor), height unchanged."""
    _, _, bx, by = _baseline(text, fs, rot, start_along_px, center_cross_px, dpi)
    point = display_point_to_page(fitz.Point(bx, by), page)
    rotate = insert_rotate(rot, page.rotation)
    morph = None
    if squeeze < 0.999:
        morph = (point, fitz.Matrix(squeeze, 1) if rotate % 180 == 0 else fitz.Matrix(1, squeeze))
    page.insert_text(point, text, fontname=fontname, fontsize=fs, rotate=rotate, color=color, overlay=True,
                     morph=morph)
    return line_rect(font, text, fs, rot, start_along_px, center_cross_px, dpi, squeeze)


def set_to_unicode(doc, page, written) -> None:
    """Give each inserted font a text layer that reads back what was written.

    MuPDF maps every glyph back to the last code point that uses it, and some
    system fonts draw the space and the no-break space (U+00A0), or the hyphen and
    the soft hyphen (U+00AD), with one glyph (Arial on macOS), so the page's text
    read 「Roof\\xa0plan」 and 「1\\xadhour」, which breaks copying and searching the
    translated drawing.  The map is rebuilt from the characters actually written
    (glyph ids are character ids under Identity-H); a glyph two of them share reads
    as the lower code point.  It also drops the map's entries for the thousands of
    glyphs never used.  ``written``: resource name -> (fitz.Font, chars)."""
    for f in page.get_fonts(full=True):
        xref, name = f[0], f[4]
        if name not in written:
            continue
        font, chars = written[name]
        table: dict[int, str] = {}
        for ch in sorted(chars):
            gid = font.has_glyph(ord(ch))
            if gid:
                table.setdefault(gid, ch)
        pairs = [f"<{gid:04x}> <{table[gid].encode('utf-16-be').hex()}>" for gid in sorted(table)]
        blocks = "".join(f"{len(chunk)} beginbfchar\n" + "\n".join(chunk) + "\nendbfchar\n"
                         for chunk in (pairs[i:i + 100] for i in range(0, len(pairs), 100)))
        cmap = ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
                "/CIDSystemInfo <</Registry(Adobe)/Ordering(UCS)/Supplement 0>> def\n"
                "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
                "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
                f"{blocks}endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n")
        kind, value = doc.xref_get_key(xref, "ToUnicode")
        if kind == "xref":
            target = int(value.split()[0])
        else:
            target = doc.get_new_xref()
            doc.update_object(target, "<<>>")
            doc.xref_set_key(xref, "ToUnicode", f"{target} 0 R")
        doc.update_stream(target, cmap.encode("ascii"))


# ----------------------------------------------------------------- main flow

def redaction_rect(row, page, dpi):
    box = row["display_box"]
    x0, y0, x1, y1 = map(float, box)
    rot = int(row.get("display_rotation", 0))
    thick = (y1 - y0) if rot % 180 == 0 else (x1 - x0)
    if row.get("origin") == "native":
        pa, pc = 0.5, 0.5
    else:
        # outline glyphs: descenders/accents may stick out of a tight OCR box
        pa, pc = max(2.0, thick * 0.12), max(2.0, thick * 0.28)
    if rot % 180 == 0:
        disp = (x0 - pa, y0 - pc, x1 + pa, y1 + pc)
    else:
        disp = (x0 - pc, y0 - pa, x1 + pc, y1 + pa)
    return disp, display_pt_to_page(px_to_display_pt(disp, dpi), page)


def _passes(rects):
    """Split rectangles into groups whose members do not overlap.

    MuPDF judges "covered" against the first redaction that touches a path,
    so a glyph straddling a neighbour's rectangle survives when overlapping
    redactions are applied together.  Non-overlapping passes avoid that.
    """
    groups: list[list] = []
    for r in rects:
        for g in groups:
            if not any(r.intersects(o) for o in g):
                g.append(r)
                break
        else:
            groups.append([r])
    return groups


# MuPDF drops a glyph once a text redaction covers more than a tenth of the glyph's
# box (the font's ascender to descender, the same box as the text layer's line box);
# measured on PyMuPDF 1.28: 1.4 pt of a 13.7 pt box goes, 1.35 pt stays.
_GLYPH_TOUCH = 0.1


def _clear_of_kept_text(disp, lb, rot, kept, dpi):
    """``disp`` (display px) pulled back off kept text lines it reaches into.

    MuPDF drops every glyph a text redaction touches, so on tightly stacked lines
    (or a label set right against a type code) the full line box of a replaced
    row also took letters of the line above or beside it: 「500门」 under
    「5*TN3000」 left only 「5」「0」 of the code.

    Across the line the rectangle stops at the kept line's box.  Fonts with tall
    boxes (Arial Unicode on macOS) make neighbouring boxes overlap far into each
    other; then it may reach just under a tenth into the kept box (_GLYPH_TOUCH),
    which MuPDF lets stand.  Either way a quarter of the row's own box stays
    covered, which still takes every glyph of its own; only when the lines overlap
    even more does it fall back to the middle of its own line."""
    x0, y0, x1, y1 = disp
    eps = 0.3 * dpi / 72.0
    horizontal = rot % 180 == 0
    # cross axis: across the line; along axis: the reading direction
    c0, c1 = (lb[1], lb[3]) if horizontal else (lb[0], lb[2])
    a0, a1 = (lb[0], lb[2]) if horizontal else (lb[1], lb[3])
    core0, core1 = c0 + 0.3 * (c1 - c0), c1 - 0.3 * (c1 - c0)
    ends0, ends1 = a0 + 0.15 * (c1 - c0), a1 - 0.15 * (c1 - c0)
    lo_c, hi_c = (y0, y1) if horizontal else (x0, x1)
    lo_a, hi_a = (x0, x1) if horizontal else (y0, y1)
    clear = [lo_c, hi_c]      # off the kept boxes altogether
    near = [lo_c, hi_c]       # just under a tenth into them
    for k in kept:
        if k[0] >= x1 or k[2] <= x0 or k[1] >= y1 or k[3] <= y0:
            continue
        k_c0, k_c1 = (k[1], k[3]) if horizontal else (k[0], k[2])
        k_a0, k_a1 = (k[0], k[2]) if horizontal else (k[1], k[3])
        mid = (k_c0 + k_c1) / 2
        reach = 0.8 * _GLYPH_TOUCH * (k_c1 - k_c0)
        if mid < core0:          # a line on one side across the reading direction
            clear[0], near[0] = max(clear[0], k_c1 + eps), max(near[0], k_c1 - reach)
        elif mid > core1:
            clear[1], near[1] = min(clear[1], k_c0 - eps), min(near[1], k_c0 + reach)
        elif (k_a0 + k_a1) / 2 < (a0 + a1) / 2:   # on the same line, before or after it
            lo_a = max(lo_a, min(k_a1 + eps, ends0))
        else:
            hi_a = min(hi_a, max(k_a0 - eps, ends1))
    for lo, hi in (clear, near):
        if min(hi, c1) - max(lo, c0) >= 0.25 * (c1 - c0):
            lo_c, hi_c = lo, hi
            break
    else:
        lo_c, hi_c = max(lo_c, min(near[0], core0)), min(hi_c, max(near[1], core1))
    return (lo_a, lo_c, hi_a, hi_c) if horizontal else (lo_c, lo_a, hi_c, hi_a)


def _kept_native(rows, removed_ids) -> list:
    """Display boxes of the text-layer lines that stay on the page."""
    return [b for r in rows if r.get("origin") == "native" and r["id"] not in removed_ids
            for b in (r.get("line_boxes") or [r["display_box"]])]


def apply_removal(page, rows, dpi, protect=None, kept_text=None):
    """Remove the rows' source text.  ``kept_text`` are the display boxes of text
    lines that stay; native redactions are kept off them (_clear_of_kept_text)."""
    native, outline = [], []
    for r in rows:
        rot = int(r.get("display_rotation", 0))
        for lb in r.get("line_boxes") or [r["display_box"]]:
            disp, rect = redaction_rect(dict(r, display_box=lb), page, dpi)
            if r.get("origin") == "native":
                if kept_text:
                    disp = _clear_of_kept_text(disp, lb, rot, kept_text, dpi)
                    rect = display_pt_to_page(px_to_display_pt(disp, dpi), page)
                native.append(rect)
            else:
                outline.append(rect)
    for g in _passes(native):
        for rect in g:
            page.add_redact_annot(rect, fill=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)
    for g in _passes(outline):
        for rect in g:
            page.add_redact_annot(rect, fill=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=fitz.PDF_REDACT_TEXT_NONE)
    if outline:
        extra = straddlers(page, rows, dpi, protect or [])
        for g in _passes(extra):
            for rect in g:
                page.add_redact_annot(rect, fill=False)
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                  graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                                  text=fitz.PDF_REDACT_TEXT_NONE)


def straddlers(page, rows, dpi, protect):
    """Glyph pieces sticking out of a removed line (OCR box cut a letter).

    A remaining path qualifies when it is glyph-sized, at least half of it lies
    inside a removed row's rectangle, and it touches no kept text row.
    """
    k = 72.0 / dpi
    zones, prot = [], []
    for r in rows:
        if r.get("origin") == "native":
            continue
        rot = int(r.get("display_rotation", 0))
        for lb in r.get("line_boxes") or [r["display_box"]]:
            thick = (lb[3] - lb[1]) if rot % 180 == 0 else (lb[2] - lb[0])
            zones.append((redaction_rect(dict(r, display_box=lb), page, dpi)[1], thick * k))
    for b in protect:
        prot.append(display_pt_to_page(px_to_display_pt(b, dpi), page))
    out = []
    for d in page.get_drawings():
        pr = d["rect"]
        if pr.width < 0.2 or pr.height < 0.2:
            continue  # hairlines: never guess, they may be drawing lines
        for z, th in zones:
            if not pr.intersects(z) or z.contains(pr):
                continue
            if max(pr.width, pr.height) > 1.6 * th:
                continue
            inter = (pr & z)
            area = max(pr.width * pr.height, 1e-6)
            if inter.width * inter.height / area < 0.5:
                continue
            if any(pr.intersects(q) for q in prot):
                continue
            out.append(fitz.Rect(pr.x0 - 0.3, pr.y0 - 0.3, pr.x1 + 0.3, pr.y1 + 0.3))
            break
    return out


def path_sigs(page, keep=None):
    from collections import Counter
    sigs, rects = Counter(), {}
    for d in page.get_drawings():
        r = d["rect"]
        key = (d.get("type"), tuple(round(v, 1) for v in (r.x0, r.y0, r.x1, r.y1)),
               tuple(round(c, 2) for c in (d.get("color") or ())), tuple(round(c, 2) for c in (d.get("fill") or ())),
               round(float(d.get("width") or 0), 2), len(d["items"]))
        sigs[key] += 1
        rects[key] = r
        if keep is not None:
            keep.setdefault(key, d)
    return sigs, rects


def _draw_items(page, d, items) -> None:
    sh = page.new_shape()
    for it in items:
        if it[0] == "l":
            sh.draw_line(it[1], it[2])
        elif it[0] == "c":
            sh.draw_bezier(it[1], it[2], it[3], it[4])
        elif it[0] == "re":
            sh.draw_rect(it[1])
        elif it[0] == "qu":
            sh.draw_quad(it[1])
    cap = d.get("lineCap")
    if isinstance(cap, (tuple, list)):
        cap = max(cap) if cap else 0
    sh.finish(color=d.get("color"), fill=d.get("fill"), width=float(d.get("width") or 0),
              dashes=d.get("dashes") or None, even_odd=bool(d.get("even_odd")),
              closePath=bool(d.get("closePath")), lineCap=int(cap or 0), lineJoin=int(d.get("lineJoin") or 0),
              fill_opacity=d.get("fill_opacity") if d.get("fill_opacity") is not None else 1,
              stroke_opacity=d.get("stroke_opacity") if d.get("stroke_opacity") is not None else 1)
    sh.commit(overlay=True)


def _item_geom(it):
    if it[0] == "re":
        r = it[1]
        pts = [r.tl, r.br]
    elif it[0] == "qu":
        q = it[1]
        pts = [q.ul, q.ur, q.ll, q.lr]
    else:
        pts = [p for p in it[1:] if isinstance(p, fitz.Point)]
    xs, ys = [p.x for p in pts], [p.y for p in pts]
    mid = fitz.Point((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    return mid, math.hypot(max(xs) - min(xs), max(ys) - min(ys))


def removal_zones(rows, page, dpi):
    """Removal rectangles (page pt, small tolerance) with their text height."""
    out = []
    k = 72.0 / dpi
    for r in rows:
        rot = int(r.get("display_rotation", 0))
        for lb in r.get("line_boxes") or [r["display_box"]]:
            a = redaction_rect(dict(r, display_box=lb), page, dpi)[1]
            thick = ((lb[3] - lb[1]) if rot % 180 == 0 else (lb[2] - lb[0])) * k
            out.append((r["id"], fitz.Rect(a.x0 - 0.5, a.y0 - 0.5, a.x1 + 0.5, a.y1 + 0.5), thick))
    return out


def replay_non_glyph(page, src_sigs, src_draw, rows, dpi) -> list:
    """Put back long strokes that sat inside a removed text line.

    MuPDF removes every sub-path lying inside a redaction rectangle, so an
    underline, a short frame edge or the end of a leader drawn in the same
    path as the label disappears with the letters.  Pieces outside the
    rectangles are never touched.  For every source path that is gone or
    changed, the pieces inside a removed line that are longer than a letter
    stroke (> 1.6 x text height) are redrawn with the original geometry and
    style; letter strokes stay deleted.
    """
    zones = removal_zones(rows, page, dpi)
    removed = src_sigs[0] - path_sigs(page)[0]
    done = []
    for key in removed:
        d = src_draw[key]
        keep = []
        for it in d["items"]:
            mid, length = _item_geom(it)
            if any(z.contains(mid) and length > 1.6 * th for _, z, th in zones):
                keep.append(it)
        if keep:
            _draw_items(page, d, keep)
            done.append({"rect": [round(v, 1) for v in d["rect"]], "items_kept": len(keep),
                         "items_total": len(d["items"])})
    return done


def is_column_aligned(row, rows):
    """Left-anchored when another line starts at the same position (lists, notes)."""
    rot = int(row.get("display_rotation", 0))
    s, _, c, t = slot_geometry(row["display_box"], rot)
    for o in rows:
        if o is row or int(o.get("display_rotation", 0)) != rot:
            continue
        s2, _, c2, t2 = slot_geometry(o["display_box"], rot)
        if abs(s - s2) <= max(3, 0.3 * t) and 0 < abs(c - c2) <= 6 * max(t, t2):
            return True
    return False


def row_em(row, dpi) -> float:
    if row.get("font_size_pt"):
        return float(row["font_size_pt"])
    rot = int(row.get("display_rotation", 0))
    k = dpi / 72.0
    ws = row.get("words") or []
    # per-word estimates: brackets and stray symbols make line boxes too tall
    ems = [em_from_box(w["text"], ((w["box"][3] - w["box"][1]) if rot % 180 == 0 else (w["box"][2] - w["box"][0])) / k)
           for w in ws if sum(c.isalnum() for c in w["text"]) >= 3 and not any(c in "()[]{}|/" for c in w["text"])]
    if ems:
        return statistics.median(ems)
    thick = statistics.median(slot_geometry(b, rot)[3] for b in (row.get("line_boxes") or [row["display_box"]]))
    return em_from_box(row.get("source_text", ""), thick / k)


def smooth_em(rows, dpi):
    """Lines of one text block share a size: snap outliers to their siblings."""
    for r in rows:
        r["_em0"] = row_em(r, dpi)
    for r in rows:
        rot = int(r.get("display_rotation", 0))
        s, _, c, t = slot_geometry(r["display_box"], rot)
        sib = [r["_em0"]]
        for o in rows:
            if o is r or int(o.get("display_rotation", 0)) != rot or o.get("origin") != r.get("origin"):
                continue
            s2, _, c2, t2 = slot_geometry(o["display_box"], rot)
            if abs(s - s2) <= max(4, 0.5 * t) and abs(c - c2) <= 5 * max(t, t2) and 0.6 <= o["_em0"] / r["_em0"] <= 1.6:
                sib.append(o["_em0"])
        med = statistics.median(sib)
        r["_em"] = med if len(sib) >= 3 and abs(r["_em0"] - med) > 0.1 * med else r["_em0"]


def _fit_ladder(fit: FitSettings) -> tuple[list[float], list[float]]:
    """Overall scales tried in the row's own line(s), then when wrapping into blank
    lines below -- CAD's rule (core.cad_layout.fit_translated_texts): full size if
    it fits, else the default scale, then 0.1 a step down to the minimum; only a
    row that does not fit even at the minimum wraps, at the largest scale it can."""
    tiers = fit.tiers()
    return [1.0] + [t for t in tiers if t < 1.0 - 1e-9], tiers


def _band_below(box, union, rot, off, thick_scale=1.0):
    """A line slot ``off`` px below ``box`` (against the up vector), as long as the
    whole block ``union`` along the reading direction, ``thick_scale`` times as thick."""
    (ux, uy) = adv_up(rot)[1]
    x0, y0, x1, y1 = box
    if rot % 180 == 0:
        x0, x1 = union[0], union[2]
        mid, half = (y0 + y1) / 2, (y1 - y0) * thick_scale / 2
        y0, y1 = mid - half, mid + half
    else:
        y0, y1 = union[1], union[3]
        mid, half = (x0 + x1) / 2, (x1 - x0) * thick_scale / 2
        x0, x1 = mid - half, mid + half
    return (x0 - ux * off, y0 - uy * off, x1 - ux * off, y1 - uy * off)


def layout_row(row, layout, page, dpi, all_rows, fit: FitSettings | None = None):
    fit = (fit or DEFAULT_FIT_SETTINGS_ZH).normalized()
    font = row["_font"].font
    rot = int(row.get("display_rotation", 0))
    lines = row.get("line_boxes") or [row["display_box"]]
    geo = [slot_geometry(b, rot) for b in lines]
    thick_px = statistics.median(g[3] for g in geo)
    k = dpi / 72.0
    target = row["_base"]
    text = row["translation"].strip()
    multi = len(lines) > 1
    if multi:
        anchor_left = row.get("line_align") != "centre"
    elif is_column_aligned(row, all_rows):
        anchor_left = True
    else:
        # centred only when the text sits midway between two bounding inks
        # (table cell, framed title); free-standing labels keep their start.
        s0, ln0, _, t0 = geo[0]
        lim = max(ln0 * 3, t0 * 40)
        f = layout.extend(lines[0], rot, True, limit_px=lim)
        b = layout.extend(lines[0], rot, False, limit_px=lim)
        bounded = f < lim - 1 and b < lim - 1
        anchor_left = not (bounded and abs(f - b) <= 0.25 * max(f, b) + t0)
    # available length per slot (px), extended into verified blank space
    slots = []
    for b, (s, ln, c, t) in zip(lines, geo):
        fwd = layout.extend(b, rot, True, limit_px=max(ln * 2.5, t * 30))
        back = 0.0 if anchor_left else layout.extend(b, rot, False, limit_px=max(ln * 1.0, t * 10))
        slots.append({"box": b, "start": s, "len": ln, "center": c, "fwd": fwd, "back": back})
    # verified blank lines below the last line take the wrapped rest (one-line labels
    # and paragraphs alike), at the source line pitch, closer when the text is smaller
    union = (min(b[0] for b in lines), min(b[1] for b in lines), max(b[2] for b in lines), max(b[3] for b in lines))
    centres = [g[2] for g in geo]
    pitch = abs(centres[-1] - centres[0]) / (len(centres) - 1) if multi else thick_px * 1.25
    pitch = max(pitch, thick_px * 1.1)
    extra_cache: dict[float, list] = {}

    def extra_lines(height_scale):
        if height_scale not in extra_cache:
            extra = []
            # a paragraph may grow by as many lines as it has (at least two)
            limit = min(MAX_EXTRA_LINES, max(2, len(lines))) if multi else LABEL_EXTRA_LINES
            for n in range(1, limit + 1):
                nb = _band_below(lines[-1], union, rot, pitch * height_scale * n, height_scale)
                if not layout.blank(nb):
                    break
                s0, ln, c0, t0 = slot_geometry(nb, rot)
                # each new line may grow only into blank space of its own
                fwd = layout.extend(nb, rot, True, limit_px=max(ln * 2.5, t0 * 30))
                back = 0.0 if anchor_left else layout.extend(nb, rot, False, limit_px=max(ln * 1.0, t0 * 10))
                extra.append({"box": nb, "start": s0, "len": ln, "center": c0, "fwd": fwd, "back": back})
            extra_cache[height_scale] = extra
        return extra_cache[height_scale]

    def try_fit(fs, squeeze, use_extra, height_scale=1.0):
        used = slots + extra_lines(height_scale) if use_extra else list(slots)
        widths = [(s["len"] + s["fwd"] + s["back"]) / k for s in used]
        out = wrap(text, font, fs, widths, squeeze)
        return (out, used) if out else (None, None)

    sign = 1 if rot in (0, 270) else -1

    def inside(start, s, width_px):
        """``start`` moved just enough to keep the line inside its verified blank band."""
        lo = s["start"] - sign * s["back"]
        hi = s["start"] + sign * (s["len"] + s["fwd"])
        if sign > 0:
            return max(min(start, hi - width_px), lo)
        return min(max(start, hi + width_px), lo)

    def place(fs, squeeze, res, used):
        widths = [font.text_length(line, fontsize=fs) * squeeze * k for line in res]
        shift = 0.0
        if not anchor_left:
            # a centred stack moves as one block: the line that must move furthest to
            # stay in its band takes the others along, so wrapped lines share one axis
            for line_w, s in zip(widths, used):
                start = s["start"] + sign * s["len"] / 2 - sign * line_w / 2
                d = inside(start, s, line_w) - start
                if abs(d) > abs(shift):
                    shift = d
        out = []
        for i, line in enumerate(res):
            s = used[i]
            width_px = widths[i]
            if anchor_left:
                start = s["start"]
            else:
                centre = s["start"] + sign * s["len"] / 2
                start = inside(centre - sign * width_px / 2 + shift, s, width_px)
            centre_x = s["center"]
            if multi and not anchor_left and len(res) < len(slots):
                # centred stack that now needs fewer lines: keep it centred on the block
                cs = [q["center"] for q in slots]
                step = (cs[-1] - cs[0]) / (len(cs) - 1)
                centre_x = (cs[0] + cs[-1]) / 2 + (i - (len(res) - 1) / 2) * step
            out.append((line, start, centre_x))
        return out

    def collides(fs, squeeze, placements):
        for line, start, centre in placements:
            r = line_rect(font, line, fs, rot, start, centre, dpi, squeeze)
            if any(not (r[2] <= a or r[0] >= c or r[3] <= b or r[1] >= d) for a, b, c, d in layout.occupied):
                return True
        return False

    one_line, wrapped = _fit_ladder(fit)
    stages = [(False, one_line, None)]
    if extra_lines(1.0) or extra_lines(scale_parts(wrapped[-1])[0]):
        stages.append((True, wrapped, "wrapped_into_blank"))
    chosen = None
    for use_extra, scales, label in stages:
        for scale in scales:
            height_scale, squeeze = scale_parts(scale)
            fs = target * height_scale
            if fs < MIN_FONT_PT:
                break
            res, used = try_fit(fs, squeeze, use_extra, height_scale)
            if res:
                pl = place(fs, squeeze, res, used)
                if not collides(fs, squeeze, pl):
                    chosen = (scale, fs, squeeze, pl, label or ("fit" if scale >= 1.0 else "shrunk"))
                    break
        if chosen:
            break
    if chosen is None:
        return None, "no_fit"
    scale, fs, squeeze, placements, how = chosen
    return {"fontsize": fs, "scale": scale, "squeeze": squeeze, "lines": placements, "how": how,
            "target": target, "anchor_left": anchor_left}, None


def row_base_size(row) -> float:
    """The 100 % size of a row's translation (pt): the source size, and for Chinese,
    Japanese or Korean written over Latin source a little smaller, so the glyphs
    stand as tall as the source capitals."""
    em = row["_em"]
    if not has_cjk(row.get("translation", "")) or has_cjk(row.get("source_text", "")):
        return em
    letters = [c for c in row.get("source_text", "") if c.isalpha()]
    caps = bool(letters) and all(not c.islower() for c in letters)
    return (0.86 if caps else 0.82) * em


def pick_fonts(rows, fonts: FontSet) -> dict[str, str]:
    """Choose each row's font; rows nothing can show are returned as skipped."""
    skipped = {}
    for r in rows:
        text = r["translation"].strip()
        if needs_shaping(text):
            skipped[r["id"]] = "unsupported_script"
            continue
        native = r.get("origin") == "native"
        choice = fonts.pick(text, bold=native and looks_bold(r.get("font", ""), r.get("font_flags", 0)),
                            serif=native and looks_serif(r.get("font", ""), r.get("font_flags", 0)))
        if choice is None:
            skipped[r["id"]] = "font_missing_glyph"
            continue
        r["_font"] = choice
    return skipped


def run(source: Path, manifest: Path, output: Path, preview: Path, report: Path, font_path: Path | None = None,
        trial_preview: Path | None = None, *, fonts: FontSet | None = None, fit: FitSettings | None = None) -> dict:
    """Write the manifest's translations into ``output``.  ``fonts`` picks the font of
    each line (``font_path`` forces one font for all); ``fit`` is the shrink ladder
    (default: the Chinese one, full size first)."""
    fonts = fonts or FontSet(forced=font_path)
    fit = (fit or DEFAULT_FIT_SETTINGS_ZH).normalized()
    data = read_json(manifest)
    dpi = float(data.get("dpi", 200))
    rows = data["rows"]
    cand = [r for r in rows if r.get("decision") == "write" and str(r.get("translation", "")).strip()
            and r.get("translation", "").strip() != r.get("source_text", "").strip()]
    cand_ids = {r["id"] for r in cand}
    skipped = {}
    for r in rows:
        if r["id"] not in cand_ids:
            skipped[r["id"]] = r.get("decision_reason") or r.get("decision") or "not_write"
    # a line no installed font can show (or that needs shaping) keeps its source
    no_font = pick_fonts(cand, fonts)
    skipped.update(no_font)
    cand = [r for r in cand if r["id"] not in no_font]
    cand_ids = {r["id"] for r in cand}

    src_doc = fitz.open(source)
    src_page = src_doc[0]
    src_rgb = render(src_page, dpi)
    src_gray = src_rgb.convert("L")
    for r in cand:
        r["_color"] = r.get("color") or sample_color(src_rgb, r["display_box"])

    # 1) trial removal on a copy; repeat while a row would alter real graphics
    protect = [r["display_box"] for r in rows if r["id"] not in cand_ids and r.get("origin") == "ocr"]
    src_draw: dict = {}
    src_sigs = path_sigs(src_page, src_draw)
    active = list(cand)
    trial = fitz.open(source)
    tp = trial[0]
    active_ids = {r["id"] for r in active}
    apply_removal(tp, active, dpi, protect, kept_text=_kept_native(rows, active_ids))
    replay_non_glyph(tp, src_sigs, src_draw, active, dpi)
    trial_gray = render(tp, dpi).convert("L")
    if trial_preview:
        trial_gray.save(trial_preview)
    ok = []
    for r in active:
        # 亮色字只在深色底上按亮墨迹算（深色标题栏里的白字）；白底上的黄字、青字
        # （CAD 常见）照常按深色墨迹算，否则整片白底都会被当成残留。
        light = is_light(r["_color"]) and dark_ground(src_gray, r["display_box"])
        si = sum(ink_count(src_gray, b, light)[0] for b in (r.get("line_boxes") or [r["display_box"]]))
        ti = sum(ink_count(trial_gray, b, light)[0] for b in (r.get("line_boxes") or [r["display_box"]]))
        r["_residual"] = (ti / si) if si else 0.0
        if si and ti / si > RESIDUAL_LIMIT:
            skipped[r["id"]] = "source_not_removable"
        else:
            ok.append(r)
    trial.close()

    # 2) real removal for removable rows only, then render for blank-space checks
    doc = fitz.open(source)
    page = doc[0]
    n_draw_src = len(src_page.get_drawings())
    n_img_src = len(src_page.get_images(full=True))
    ok_ids = {r["id"] for r in ok}
    apply_removal(page, ok, dpi, protect + [r["display_box"] for r in cand if r["id"] not in ok_ids],
                  kept_text=_kept_native(rows, ok_ids))
    replayed = replay_non_glyph(page, src_sigs, src_draw, ok, dpi)
    n_restored = len(replayed)
    after_gray = render(page, dpi).convert("L")
    layout = Layout(after_gray, dpi, bounds=content_box(src_gray, pad_px=2 * dpi / 72.0,
                                                        rows=[b for r in rows for b in (r.get("line_boxes") or [r["display_box"]])]))
    smooth_em(ok, dpi)
    for r in ok:
        r["_base"] = row_base_size(r)
    inserted: set[str] = set()
    placed, failed = [], []
    # big text first so titles keep their size when space is contested
    for r in sorted(ok, key=lambda r: -max(slot_geometry(b, int(r.get("display_rotation", 0)))[3]
                                           for b in (r.get("line_boxes") or [r["display_box"]]))):
        plan, why = layout_row(r, layout, page, dpi, cand, fit)
        if plan is None:
            failed.append(r)
            skipped[r["id"]] = why
            continue
        choice = r["_font"]
        if choice.name not in inserted:
            page.insert_font(fontname=choice.name, fontfile=str(choice.path))
            inserted.add(choice.name)
        rot = int(r.get("display_rotation", 0))
        rects = []
        for line, start, centre in plan["lines"]:
            rects.append(place_line(page, choice.font, line, plan["fontsize"], rot, start, centre, dpi, r["_color"],
                                    fontname=choice.name, squeeze=plan["squeeze"]))
        layout.occupied.extend(rects)
        r["_plan"] = plan
        r["_rects"] = rects
        placed.append(r)

    # removed rows that could not be laid out would lose their text: restore by
    # rebuilding without them (never leave a hole)
    if failed:
        doc.close()
        keep_ids = {r["id"] for r in placed}
        return run_restricted(source, manifest, output, preview, report, keep_ids, skipped, data, fonts, fit)

    written: dict[str, tuple[object, set[str]]] = {}
    for r in placed:
        written.setdefault(r["_font"].name, (r["_font"].font, set()))[1].update(
            "".join(ln[0] for ln in r["_plan"]["lines"]))
    set_to_unicode(doc, page, written)
    doc.subset_fonts()
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output, garbage=3, deflate=True)
    n_draw_out = len(page.get_drawings())
    doc.close()
    chk = fitz.open(output)
    render(chk[0], dpi).save(preview)
    out_info = {"rect": list(chk[0].rect), "rotation": chk[0].rotation, "images": len(chk[0].get_images(full=True))}
    chk.close()
    rep = {
        "source": str(source), "source_sha256": sha256(source), "manifest": str(manifest), "output": str(output),
        "output_sha256": sha256(output), "dpi": dpi,
        "metrics": {
            "rows_total": len(rows), "candidates": len(cand), "written": len(placed),
            "skipped": len(skipped), "drawings_source": n_draw_src, "drawings_output": n_draw_out,
            "images_source": n_img_src, "images_output": out_info["images"], "paths_restored": n_restored,
            "fit_modes": _count(p["_plan"]["how"] for p in placed),
            "font_ratio_median": statistics.median(p["_plan"]["fontsize"] / p["_plan"]["target"] for p in placed) if placed else None,
            "scale_median": statistics.median(p["_plan"]["scale"] for p in placed) if placed else None,
            "fonts": sorted({p["_font"].path.name for p in placed}),
        },
        "page": out_info,
        "written": [{"id": r["id"], "source_text": r["source_text"], "translation": r["translation"],
                     "translation_source": r.get("translation_source"), "fontsize": round(r["_plan"]["fontsize"], 2),
                     "target_fontsize": round(r["_plan"]["target"], 2), "fit": r["_plan"]["how"],
                     "scale": r["_plan"]["scale"], "width_factor": round(r["_plan"]["squeeze"], 3),
                     "font": r["_font"].path.name,
                     "anchor_left": r["_plan"]["anchor_left"], "lines": [ln[0] for ln in r["_plan"]["lines"]],
                     "residual_ink": round(r["_residual"], 3), "color": [round(c, 3) for c in r["_color"]],
                     "display_box": r["display_box"], "line_boxes": r.get("line_boxes"),
                     "written_rects_px": [[round(v, 1) for v in x] for x in r["_rects"]],
                     "display_rotation": r.get("display_rotation", 0), "origin": r.get("origin")} for r in placed],
        "skipped": [{"id": k, "reason": v} for k, v in skipped.items()],
        "replayed_paths": replayed,
    }
    write_json(report, rep)
    return rep


def run_restricted(source, manifest, output, preview, report, keep_ids, skipped, data, fonts, fit):
    rows = data["rows"]
    for r in rows:
        if r.get("decision") == "write" and r["id"] not in keep_ids:
            r["decision"] = "keep_source"
            r["decision_reason"] = skipped.get(r["id"], "layout_failed")
        # working values of the first attempt (fonts, plans) are not part of the manifest
        for key in [key for key in r if key.startswith("_")]:
            del r[key]
    tmp = Path(report).with_name(Path(report).stem + "_restricted_manifest.json")
    write_json(tmp, data)
    rep = run(source, tmp, output, preview, report, fonts=fonts, fit=fit)
    return rep


def _count(it):
    out = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--preview", type=Path, required=True)
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--font", type=Path, required=True)
    ap.add_argument("--trial-preview", type=Path)
    a = ap.parse_args()
    for p in (a.source, a.manifest, a.font):
        if not p.exists():
            raise SystemExit(f"missing: {p}")
    if a.output.resolve() == a.source.resolve():
        raise SystemExit("refusing to overwrite the source PDF")
    rep = run(a.source, a.manifest, a.output, a.preview, a.report, a.font, a.trial_preview)
    print({k: rep["metrics"][k] for k in rep["metrics"]})


if __name__ == "__main__":
    main()
