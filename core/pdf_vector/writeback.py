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

Chinese size is derived from the source glyph height, not squeezed into the
tight OCR rectangle.  Lines may extend into verified blank space; they shrink
only when that space runs out, and wrap only into the source line slots or
verified blank lines.  Colour is sampled from the source ink.
"""
from __future__ import annotations

import argparse
import math
import re
import statistics
from pathlib import Path

import fitz
from PIL import Image

from .common import (
    adv_up, display_point_to_page, display_pt_to_page, insert_rotate,
    px_to_display_pt, read_json, sha256, write_json,
)

Image.MAX_IMAGE_PIXELS = None
FONT_NAME = "CNF"
INK_GRAY = 200            # gray level counted as ink
RESIDUAL_LIMIT = 0.35     # share of source ink allowed to survive redaction
MIN_SHRINK = 0.55         # shrink factor tried before wrapping
FLOOR_SHRINK = 0.38       # absolute smallest size relative to the target
MIN_FONT_PT = 1.6


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

TOKEN_RE = re.compile(r"[A-Za-z0-9À-ÿ.,:;=/+\-×²³%#°()\[\]'’&_<>≤≥Ø]+|\s+|.", re.S)


def wrap(text: str, font: fitz.Font, fs: float, widths: list[float]) -> list[str] | None:
    """Greedy wrap into len(widths) lines; Latin/number runs never split."""
    toks = TOKEN_RE.findall(text)
    lines, cur, i = [], "", 0
    for t in toks:
        w_lim = widths[min(i, len(widths) - 1)]
        cand = cur + t
        if font.text_length(cand.rstrip(), fontsize=fs) <= w_lim or not cur.strip():
            if font.text_length(cand.rstrip(), fontsize=fs) > w_lim and not cur.strip():
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

def content_box(gray: Image.Image, pad_px: float = 0.0) -> tuple[float, float, float, float] | None:
    """Outermost ink of the source page (px). Translations never grow past it:
    a paragraph on a text page would otherwise run into the page margin."""
    mask = gray.point(lambda v: 255 if v < 200 else 0)
    box = mask.getbbox()
    if box is None:
        return None
    x0, y0, x1, y1 = box
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


def _baseline(font, text, fs, rot, start_along_px, center_cross_px, dpi):
    k = 72.0 / dpi
    (ax, ay), (ux, uy) = adv_up(rot)
    # CJK ink spans roughly baseline-0.12em .. baseline+0.88em; centre it.
    shift = 0.38 * fs
    if rot % 180 == 0:
        sx, sy = start_along_px * k, center_cross_px * k
    else:
        sx, sy = center_cross_px * k, start_along_px * k
    return sx, sy, sx - ux * shift, sy - uy * shift


def line_rect(font, text, fs, rot, start_along_px, center_cross_px, dpi):
    """Display px rectangle a line will occupy (no drawing)."""
    k = 72.0 / dpi
    (ax, ay), _ = adv_up(rot)
    sx, sy, bx, by = _baseline(font, text, fs, rot, start_along_px, center_cross_px, dpi)
    width_pt = font.text_length(text, fontsize=fs)
    ex, ey = bx + ax * width_pt, by + ay * width_pt
    tx0, tx1 = sorted([bx, ex])
    ty0, ty1 = sorted([by, ey])
    half = 0.5 * fs
    if rot % 180 == 0:
        return (tx0 / k, (sy - half) / k, tx1 / k, (sy + half) / k)
    return ((sx - half) / k, ty0 / k, (sx + half) / k, ty1 / k)


def place_line(page, font, text, fs, rot, start_along_px, center_cross_px, dpi, color):
    """Insert one line; return its display px rectangle."""
    _, _, bx, by = _baseline(font, text, fs, rot, start_along_px, center_cross_px, dpi)
    page.insert_text(display_point_to_page(fitz.Point(bx, by), page), text, fontname=FONT_NAME, fontsize=fs,
                     rotate=insert_rotate(rot, page.rotation), color=color, overlay=True)
    return line_rect(font, text, fs, rot, start_along_px, center_cross_px, dpi)


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


def apply_removal(page, rows, dpi, protect=None):
    native, outline = [], []
    for r in rows:
        for lb in r.get("line_boxes") or [r["display_box"]]:
            rect = redaction_rect(dict(r, display_box=lb), page, dpi)[1]
            (native if r.get("origin") == "native" else outline).append(rect)
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


def layout_row(row, layout, font, page, dpi, all_rows):
    rot = int(row.get("display_rotation", 0))
    lines = row.get("line_boxes") or [row["display_box"]]
    geo = [slot_geometry(b, rot) for b in lines]
    thick_px = statistics.median(g[3] for g in geo)
    k = dpi / 72.0
    em = row["_em"]
    letters = [c for c in row.get("source_text", "") if c.isalpha()]
    caps = bool(letters) and all(not c.islower() for c in letters)
    target = (0.86 if caps else 0.82) * em
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
    # extra blank lines below the last slot (wrap room) for single-line rows
    extra = []
    if not multi:
        b = lines[0]
        (ux, uy) = adv_up(rot)[1]
        x0, y0, x1, y1 = b
        gap = thick_px * 1.25
        for n in (1, 2):
            off = gap * n
            nb = (x0 - ux * off, y0 - uy * off, x1 - ux * off, y1 - uy * off)
            if layout.blank(nb):
                extra.append(nb)
            else:
                break

    def try_fit(fs, use_extra):
        widths = []
        for s in slots:
            avail = s["len"] + s["fwd"] + s["back"]
            widths.append(avail / k)
        all_slots = list(slots)
        if use_extra:
            for nb in extra:
                s0, ln, c0, _ = slot_geometry(nb, rot)
                all_slots.append({"box": nb, "start": s0, "len": ln, "center": c0, "fwd": slots[0]["fwd"], "back": slots[0]["back"]})
                widths.append((ln + slots[0]["fwd"] + slots[0]["back"]) / k)
        out = wrap(text, font, fs, widths[: len(all_slots)])
        return (out, all_slots) if out else (None, None)

    sign = 1 if rot in (0, 270) else -1

    def place(fs, res, used):
        out = []
        for i, line in enumerate(res):
            s = used[i]
            width_px = font.text_length(line, fontsize=fs) * k
            if anchor_left:
                start = s["start"]
            else:
                centre = s["start"] + sign * s["len"] / 2
                start = centre - sign * width_px / 2
                # keep inside the verified blank band
                lo = s["start"] - sign * s["back"]
                hi = s["start"] + sign * (s["len"] + s["fwd"])
                if sign > 0:
                    start = max(min(start, hi - width_px), lo)
                else:
                    start = min(max(start, hi + width_px), lo)
            centre_x = s["center"]
            if multi and not anchor_left and len(res) < len(slots):
                # centred stack that now needs fewer lines: keep it centred on the block
                cs = [q["center"] for q in slots]
                step = (cs[-1] - cs[0]) / (len(cs) - 1)
                centre_x = (cs[0] + cs[-1]) / 2 + (i - (len(res) - 1) / 2) * step
            out.append((line, start, centre_x))
        return out

    def collides(fs, placements):
        for line, start, centre in placements:
            r = line_rect(font, line, fs, rot, start, centre, dpi)
            if any(not (r[2] <= a or r[0] >= c or r[3] <= b or r[1] >= d) for a, b, c, d in layout.occupied):
                return True
        return False

    chosen = None
    stages = [(False, target, target * MIN_SHRINK, None)]
    if extra:
        stages.append((True, target, target * MIN_SHRINK, "wrapped_into_blank"))
    stages.append((False, target * MIN_SHRINK, max(MIN_FONT_PT / max(target, 1e-6), FLOOR_SHRINK) * target, "shrunk_hard"))
    for use_extra, hi_fs, lo_fs, label in stages:
        fs = hi_fs
        while fs >= lo_fs and fs >= MIN_FONT_PT:
            res, used = try_fit(fs, use_extra)
            if res:
                pl = place(fs, res, used)
                if not collides(fs, pl):
                    chosen = (fs, pl, label or ("fit" if fs == target else "shrunk"))
                    break
            fs *= 0.95
        if chosen:
            break
    if chosen is None:
        return None, "no_fit"
    fs, placements, how = chosen
    return {"fontsize": fs, "lines": placements, "how": how, "target": target, "anchor_left": anchor_left}, None


def run(source: Path, manifest: Path, output: Path, preview: Path, report: Path, font_path: Path,
        trial_preview: Path | None = None) -> dict:
    data = read_json(manifest)
    dpi = float(data.get("dpi", 200))
    rows = data["rows"]
    cand = [r for r in rows if r.get("decision") == "write" and str(r.get("translation", "")).strip()
            and r.get("translation", "").strip() != r.get("source_text", "").strip()]
    skipped = {}
    for r in rows:
        if r not in cand:
            skipped[r["id"]] = r.get("decision_reason") or r.get("decision") or "not_write"

    src_doc = fitz.open(source)
    src_page = src_doc[0]
    src_rgb = render(src_page, dpi)
    src_gray = src_rgb.convert("L")
    for r in cand:
        r["_color"] = r.get("color") or sample_color(src_rgb, r["display_box"])

    # 1) trial removal on a copy; repeat while a row would alter real graphics
    protect = [r["display_box"] for r in rows if r not in cand and r.get("origin") == "ocr"]
    src_draw: dict = {}
    src_sigs = path_sigs(src_page, src_draw)
    active = list(cand)
    trial = fitz.open(source)
    tp = trial[0]
    apply_removal(tp, active, dpi, protect)
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
    apply_removal(page, ok, dpi, protect + [r["display_box"] for r in cand if r not in ok])
    replayed = replay_non_glyph(page, src_sigs, src_draw, ok, dpi)
    n_restored = len(replayed)
    after_gray = render(page, dpi).convert("L")
    layout = Layout(after_gray, dpi, bounds=content_box(src_gray, pad_px=2 * dpi / 72.0))
    smooth_em(ok, dpi)
    font = fitz.Font(fontfile=str(font_path))
    page.insert_font(fontname=FONT_NAME, fontfile=str(font_path))
    placed, failed = [], []
    # big text first so titles keep their size when space is contested
    for r in sorted(ok, key=lambda r: -max(slot_geometry(b, int(r.get("display_rotation", 0)))[3]
                                           for b in (r.get("line_boxes") or [r["display_box"]]))):
        plan, why = layout_row(r, layout, font, page, dpi, cand)
        if plan is None:
            failed.append(r)
            skipped[r["id"]] = why
            continue
        rot = int(r.get("display_rotation", 0))
        rects = []
        for line, start, centre in plan["lines"]:
            rects.append(place_line(page, font, line, plan["fontsize"], rot, start, centre, dpi, r["_color"]))
        layout.occupied.extend(rects)
        r["_plan"] = plan
        r["_rects"] = rects
        placed.append(r)

    # removed rows that could not be laid out would lose their text: restore by
    # rebuilding without them (never leave a hole)
    if failed:
        doc.close()
        keep_ids = {r["id"] for r in placed}
        return run_restricted(source, manifest, output, preview, report, font_path, keep_ids, skipped, data)

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
        },
        "page": out_info,
        "written": [{"id": r["id"], "source_text": r["source_text"], "translation": r["translation"],
                     "translation_source": r.get("translation_source"), "fontsize": round(r["_plan"]["fontsize"], 2),
                     "target_fontsize": round(r["_plan"]["target"], 2), "fit": r["_plan"]["how"],
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


def run_restricted(source, manifest, output, preview, report, font_path, keep_ids, skipped, data):
    rows = data["rows"]
    for r in rows:
        if r.get("decision") == "write" and r["id"] not in keep_ids:
            r["decision"] = "keep_source"
            r["decision_reason"] = skipped.get(r["id"], "layout_failed")
    tmp = Path(report).with_name(Path(report).stem + "_restricted_manifest.json")
    write_json(tmp, data)
    rep = run(source, tmp, output, preview, report, font_path)
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
