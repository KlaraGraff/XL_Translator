"""Extract text rows from a single-page drawing PDF.

* Native text layer -> rows with exact font size / colour / direction.
* Everything else (vector outline text) -> Tesseract on the display raster
  with the native text removed first, in three reading directions
  (0 / 90 / 270), tiled so large sheets keep detail.
* OCR rows are deduplicated across tiles, passes and directions, split at
  vector rules that cross them (table dividers), then consecutive lines of
  one paragraph are grouped so the model translates whole sentences.

No drawing-specific coordinates, IDs or vocabularies are used.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import math
import os
import re
import statistics
import subprocess
import tempfile
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image

from .common import fitz, norm_text, sha256, write_json

Image.MAX_IMAGE_PIXELS = None
TILE, STEP = 1800, 1560


# ------------------------------------------------------------- native text

def clean_leaders(text: str) -> str:
    """Dotted leader lines OCR as '....cccceeee' runs; normalise them to dots."""
    import re
    return re.sub(r"[.\u2026·]{3,}\S*", lambda m: "......" if len(m.group(0)) >= 6 else m.group(0), text)


def native_rows(page: fitz.Page, dpi: float) -> list[dict]:
    k = dpi / 72.0
    rm = page.rotation_matrix
    out = []
    d = page.get_text("dict")
    for b in d["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            spans = [s for s in ln["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = norm_text("".join(s["text"] for s in ln["spans"]))
            r = fitz.Rect(ln["bbox"]) * rm
            dx, dy = ln["dir"]
            vx, vy = dx * rm.a + dy * rm.c, dx * rm.b + dy * rm.d
            rot = 0 if vx > 0.7 else 180 if vx < -0.7 else 90 if vy < -0.7 else 270 if vy > 0.7 else None
            if rot is None:
                rot = 0  # oblique native text: written horizontal, flagged
            col = spans[0]["color"]
            out.append({
                "origin": "native", "source_text": text,
                "display_box": [round(v * k, 1) for v in (r.x0, r.y0, r.x1, r.y1)],
                "display_rotation": rot, "oblique": not any(abs(v) > 0.99 for v in (vx, vy)),
                "font_size_pt": max(s["size"] for s in spans), "font": spans[0]["font"],
                "color": [((col >> 16) & 255) / 255, ((col >> 8) & 255) / 255, (col & 255) / 255],
                "ocr_score": None, "source_passes": ["native"],
            })
    return out


# ------------------------------------------------------------- OCR

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _tess(img: Image.Image, langs: str, exe: str = "tesseract") -> list[dict]:
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "t.png"
        img.save(f)
        res = subprocess.run([exe, str(f), "stdout", "--psm", "11", "-l", langs, "tsv"], creationflags=_NO_WINDOW,
                             capture_output=True, text=True)
    words = []
    for line in res.stdout.splitlines()[1:]:
        a = line.split("\t")
        if len(a) < 12 or not a[11].strip() or float(a[10]) < 0:
            continue
        x, y, w, h = map(int, a[6:10])
        words.append({"key": tuple(a[1:5]), "text": a[11], "score": float(a[10]) / 100, "box": [x, y, x + w, y + h]})
    return words


def _styled(t: str) -> bool:
    return sum(c.isalnum() for c in t) >= 3 and not any(c in "()[]{}" for c in t)


def _word_em(w) -> float:
    """Type size implied by one word box, independent of its letter shapes."""
    t = w["text"]
    h = w["box"][3] - w["box"][1]
    letters = [c for c in t if c.isalpha()]
    if not letters:
        return h / 0.72
    asc = any(c in "bdfhklt" or c.isupper() or c.isdigit() for c in t)
    desc = any(c in "gjpqy" for c in t)
    if asc and desc:
        return h / 0.95
    if desc:
        return h / 0.75
    if asc:
        return h / 0.74
    return h / 0.52


def _lines_from_words(words: list[dict]) -> list[dict]:
    groups: dict = {}
    # a vertical rule next to text is read as '|'; it is drawing, not text
    words = [w for w in words if not re.fullmatch(r"[|¦]+", w["text"])]
    for w in words:
        groups.setdefault(w["key"], []).append(w)
    out = []
    for ws in groups.values():
        ws.sort(key=lambda z: z["box"][0])
        med = statistics.median(w["box"][3] - w["box"][1] for w in ws)
        chunks = [[]]
        for w in ws:
            if chunks[-1]:
                prev = chunks[-1]
                styled = [z for z in prev if _styled(z["text"])]
                other_style = False
                if styled and _styled(w["text"]):
                    pem = statistics.median(_word_em(z) for z in styled)
                    wem = _word_em(w)
                    # a different type size is a different label ('zone' vs 'critique'
                    # differ in raw height only because of ascenders/descenders)
                    ratio = max(wem / pem, pem / wem)
                    pb = statistics.median(z["box"][3] for z in styled)
                    ph = statistics.median(z["box"][3] - z["box"][1] for z in styled)
                    shifted = abs(w["box"][3] - pb) > 0.4 * ph and not any(c in "gjpqy" for c in w["text"])
                    other_style = ratio > 1.3 or (ratio > 1.15 and shifted)
                if w["box"][0] - prev[-1]["box"][2] > 3.0 * med or other_style:
                    chunks.append([])
            chunks[-1].append(w)
        for ch in chunks:
            out.append({"words": ch})
    return out


def _map_box(box, rot, W, H, ox, oy):
    """Box in the rotated-tile image -> display px of the unrotated page."""
    x0, y0, x1, y1 = box
    x0, x1, y0, y1 = x0 + ox, x1 + ox, y0 + oy, y1 + oy
    if rot == 0:
        return [x0, y0, x1, y1]
    if rot == 90:   # image was rotated clockwise by 90 (size H x W)
        return [y0, H - x1, y1, H - x0]
    if rot == 270:  # image was rotated counter-clockwise by 90
        return [W - y1, x0, W - y0, x1]
    raise ValueError(rot)


def ocr_rows(gray: Image.Image, langs: str, rotations=(0, 90, 270), workers: int = 6,
             exe: str = "tesseract") -> list[dict]:
    W, H = gray.size
    jobs = []
    # faint grey outline lettering needs a hard threshold to be read at all
    thr = gray.point(lambda v: 0 if v < 235 else 255)
    for variant, base in (("n", gray), ("t235", thr)):
        for rot in rotations:
            img = base if rot == 0 else base.rotate(-90 if rot == 90 else 90, expand=True)
            IW, IH = img.size
            jobs.append((rot, img, 0, 0, f"{variant}_r{rot}_full"))
            for y in range(0, IH, STEP):
                for x in range(0, IW, STEP):
                    jobs.append((rot, img.crop((x, y, min(IW, x + TILE), min(IH, y + TILE))), x, y,
                                 f"{variant}_r{rot}_t{x}_{y}"))

    def work(j):
        rot, img, ox, oy, name = j
        lines = _lines_from_words(_tess(img, langs, exe))
        res = []
        full_w, full_h = (W, H) if rot == 0 else (H, W)
        tw, th = img.size
        for ln in lines:
            ws = ln["words"]
            bx0 = min(w["box"][0] for w in ws)
            by0 = min(w["box"][1] for w in ws)
            bx1 = max(w["box"][2] for w in ws)
            by1 = max(w["box"][3] for w in ws)
            # a line touching an inner tile border was probably cut in half
            m = max(3, 0.6 * (by1 - by0))
            clipped = ((bx0 <= m and ox > 0) or (by0 <= 3 and oy > 0)
                       or (bx1 >= tw - m and ox + tw < full_w) or (by1 >= th - 3 and oy + th < full_h))
            for w in ws:
                w["box"] = _map_box(w["box"], rot, W, H, ox, oy)
                w.pop("key", None)
            box = [min(w["box"][0] for w in ws), min(w["box"][1] for w in ws),
                   max(w["box"][2] for w in ws), max(w["box"][3] for w in ws)]
            res.append({"origin": "ocr", "source_text": clean_leaders(norm_text(" ".join(w["text"] for w in ws))),
                        "display_box": box, "display_rotation": rot, "words": ws,
                        "ocr_score": sum(w["score"] for w in ws) / len(ws), "source_passes": [name],
                        "clipped": clipped})
        return res

    raw = []
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for res in pool.map(work, jobs):
            raw.extend(res)
    return raw


def _overlap(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    aa = max(1, (a[2] - a[0]) * (a[3] - a[1]))
    bb = max(1, (b[2] - b[0]) * (b[3] - b[1]))
    return ix * iy / min(aa, bb)


def _similar(a, b):
    a, b = a.casefold(), b.casefold()
    return a in b or b in a or SequenceMatcher(None, a, b).ratio() > 0.6


def _quality(r):
    letters = sum(c.isalnum() for c in r["source_text"])
    return r["ocr_score"] ** 2 * (1 + math.log1p(letters)) * (0.5 if r.get("clipped") else 1.0)


def _area(b):
    return max(1, (b[2] - b[0]) * (b[3] - b[1]))


def _letters(r):
    return sum(c.isalnum() for c in r["source_text"])


def _same_start(a, b) -> bool:
    """Two reads of one line: same starting edge and same first characters."""
    rot = a["display_rotation"]
    ba, bb = a["display_box"], b["display_box"]
    thick = (ba[3] - ba[1]) if rot % 180 == 0 else (ba[2] - ba[0])
    idx = {0: 0, 180: 2, 90: 3, 270: 1}[rot]
    if abs(ba[idx] - bb[idx]) > 0.6 * thick:
        return False
    ka = "".join(c for c in a["source_text"].casefold() if c.isalnum())[:4]
    kb = "".join(c for c in b["source_text"].casefold() if c.isalnum())[:4]
    return len(ka) >= 2 and ka == kb


def _thick_ratio(a, b) -> bool:
    """Same text height (a fragment of a line, not a label inside a box)."""
    rot = a["display_rotation"]
    ta = (a["display_box"][3] - a["display_box"][1]) if rot % 180 == 0 else (a["display_box"][2] - a["display_box"][0])
    tb = (b["display_box"][3] - b["display_box"][1]) if rot % 180 == 0 else (b["display_box"][2] - b["display_box"][0])
    return 0.7 <= ta / max(tb, 1) <= 1.4


def dedup(rows: list[dict]) -> tuple[list[dict], int]:
    """Merge the same label seen by several tiles/passes/directions.

    Best reads go first.  A later read that conflicts with a kept one is
    dropped, except when it is a clearly longer read of the same place (a
    whole line versus a fragment of it): then it replaces the fragment.
    """
    rows = sorted((r for r in rows if r["source_text"].strip()), key=_quality, reverse=True)
    kept: dict[int, dict] = {}
    grid: dict = {}
    G = 400
    dropped = 0
    nid = 0

    def cells(b):
        for gx in range(int(b[0]) // G, int(b[2]) // G + 1):
            for gy in range(int(b[1]) // G, int(b[3]) // G + 1):
                yield gx, gy

    def conflict(r, k):
        ov = _overlap(r["display_box"], k["display_box"])
        same_dir = k["display_rotation"] == r["display_rotation"]
        size_ok = min(_area(r["display_box"]), _area(k["display_box"])) / max(_area(r["display_box"]), _area(k["display_box"])) >= 0.5
        ra, ka = r["source_text"].casefold(), k["source_text"].casefold()
        return ((same_dir and ov > 0.6 and _similar(r["source_text"], k["source_text"]))
                or (same_dir and ov > 0.4 and len(min(ra, ka, key=len)) >= 4 and (ra in ka or ka in ra))
                or (ov > 0.75 and size_ok) or (not same_dir and ov > 0.35)
                or (same_dir and ov > 0.4 and _same_start(r, k))
                or (same_dir and ov > 0.85 and _thick_ratio(r, k) and _area(r["display_box"]) < _area(k["display_box"]))
                or (same_dir and ov > 0.5 and _thick_ratio(r, k) and (r.get("clipped") or k.get("clipped"))))

    for r in rows:
        cand = {}
        for c in cells(r["display_box"]):
            for kid in grid.get(c, []):
                if kid in kept:
                    cand[kid] = kept[kid]
        hits = [kid for kid, k in cand.items() if conflict(r, k)]
        if not hits:
            kept[nid] = r
            for c in cells(r["display_box"]):
                grid.setdefault(c, []).append(nid)
            nid += 1
            continue
        same_dir_hits = [kid for kid in hits if kept[kid]["display_rotation"] == r["display_rotation"]]
        longer = (same_dir_hits and len(same_dir_hits) == len(hits) and r["ocr_score"] >= 0.5
                  and all((_area(r["display_box"]) >= 1.8 * _area(kept[kid]["display_box"])
                           and _letters(r) >= 1.5 * _letters(kept[kid]))
                          or (kept[kid].get("clipped") and not r.get("clipped")
                              and _area(r["display_box"]) > _area(kept[kid]["display_box"]))
                          for kid in hits))
        if longer:
            passes = set(r["source_passes"])
            for kid in hits:
                passes |= set(kept.pop(kid)["source_passes"])
                dropped += 1
            r["source_passes"] = sorted(passes)
            kept[nid] = r
            for c in cells(r["display_box"]):
                grid.setdefault(c, []).append(nid)
            nid += 1
        else:
            k = kept[hits[0]]
            k["source_passes"] = sorted(set(k["source_passes"]) | set(r["source_passes"]))
            dropped += 1
    return list(kept.values()), dropped


# ------------------------------------------------------------- vector rules

def rules(page: fitz.Page, dpi: float, min_len_px: float = 12) -> tuple[list, list]:
    """Axis-aligned vector segments in display px: (verticals, horizontals)."""
    k = dpi / 72.0
    rm = page.rotation_matrix
    V, Hs = [], []

    def add(p, q):
        p, q = p * rm, q * rm
        x0, y0, x1, y1 = p.x * k, p.y * k, q.x * k, q.y * k
        if abs(x0 - x1) < 1.5 and abs(y1 - y0) >= min_len_px:
            V.append(((x0 + x1) / 2, min(y0, y1), max(y0, y1)))
        elif abs(y0 - y1) < 1.5 and abs(x1 - x0) >= min_len_px:
            Hs.append(((y0 + y1) / 2, min(x0, x1), max(x0, x1)))

    for d in page.get_drawings():
        for it in d["items"]:
            if it[0] == "l":
                add(it[1], it[2])
            elif it[0] == "re":
                r = it[1]
                for a, b in ((r.tl, r.tr), (r.bl, r.br), (r.tl, r.bl), (r.tr, r.br)):
                    add(a, b)
            elif it[0] == "qu":
                q = it[1]
                for a, b in ((q.ul, q.ur), (q.ll, q.lr), (q.ul, q.ll), (q.ur, q.lr)):
                    add(a, b)
    return V, Hs


def split_at_rules(rows, V, Hs):
    """Split OCR lines whose words sit on both sides of a crossing rule."""
    out, n_split = [], 0
    vb: dict = {}
    hb: dict = {}
    for s in V:
        vb.setdefault(int(s[0]) // 50, []).append(s)
    for s in Hs:
        hb.setdefault(int(s[0]) // 50, []).append(s)
    for r in rows:
        ws = r.get("words")
        if r["origin"] != "ocr" or not ws or len(ws) < 2:
            out.append(r)
            continue
        rot = r["display_rotation"]
        x0, y0, x1, y1 = r["display_box"]
        ext = 0.35 * ((y1 - y0) if rot % 180 == 0 else (x1 - x0))
        if rot == 0:
            ws = sorted(ws, key=lambda w: w["box"][0])
            cuts = set()
            for i in range(len(ws) - 1):
                a, b = ws[i]["box"][2], ws[i + 1]["box"][0]
                for bk in range(int(a) // 50 - 1, int(b) // 50 + 2):
                    for (x, s0, s1) in vb.get(bk, []):
                        if a - 1 <= x <= b + 1 and s0 <= y0 - ext and s1 >= y1 + ext:
                            cuts.add(i)
        else:
            ws = sorted(ws, key=lambda w: w["box"][1] if rot == 270 else -w["box"][3])
            cuts = set()
            for i in range(len(ws) - 1):
                if rot == 270:
                    a, b = ws[i]["box"][3], ws[i + 1]["box"][1]
                else:
                    a, b = ws[i + 1]["box"][3], ws[i]["box"][1]
                for bk in range(int(a) // 50 - 1, int(b) // 50 + 2):
                    for (y, s0, s1) in hb.get(bk, []):
                        if a - 1 <= y <= b + 1 and s0 <= x0 - ext and s1 >= x1 + ext:
                            cuts.add(i)
        if not cuts:
            out.append(r)
            continue
        n_split += 1
        part = []
        for i, w in enumerate(ws):
            part.append(w)
            if i in cuts or i == len(ws) - 1:
                box = [min(z["box"][0] for z in part), min(z["box"][1] for z in part),
                       max(z["box"][2] for z in part), max(z["box"][3] for z in part)]
                out.append({**r, "source_text": norm_text(" ".join(z["text"] for z in part)), "display_box": box,
                            "words": part, "ocr_score": sum(z["score"] for z in part) / len(part),
                            "split_from_rule": True})
                part = []
    return out, n_split


# ------------------------------------------------------------- paragraphs

def _geo(box, rot):
    x0, y0, x1, y1 = box
    if rot == 0:
        return x0, y0, y1 - y0, x1 - x0      # start, cross_pos, thick, length
    if rot == 180:
        return -x1, -y1, y1 - y0, x1 - x0
    if rot == 90:
        return -y1, x0, x1 - x0, y1 - y0
    return y0, -x1, x1 - x0, y1 - y0


def _rule_between(a, b, rot, V, Hs, need_cell=False):
    """True if a rule separating two stacked lines spans both of them.

    Table row borders run across the whole cell, past both lines.  A shorter
    stroke between the lines (furniture outline, leader end) does not separate
    a two-line label.  With ``need_cell`` the border must also meet cell walls
    on both sides close to the text (a table), not just a long plan line.
    """
    if rot % 180 != 0:  # work in the reading frame: swap axes
        a, b = (a[1], a[0], a[3], a[2]), (b[1], b[0], b[3], b[2])
        V, Hs = Hs, V
    top, bot = (a, b) if a[1] < b[1] else (b, a)
    lo, hi = top[3], bot[1]
    t = min(a[3] - a[1], b[3] - b[1])
    x0, x1 = min(a[0], b[0]), max(a[2], b[2])
    for y, s0, s1 in Hs:
        if not (lo - 1 <= y <= hi + 1 and s0 <= x0 + 0.3 * t and s1 >= x1 - 0.3 * t):
            continue
        if not need_cell:
            return True
        left = any(x0 - 4 * t <= x <= x0 + 0.3 * t and v0 <= top[1] and v1 >= bot[3] for x, v0, v1 in V)
        right = any(x1 - 0.3 * t <= x <= x1 + 4 * t and v0 <= top[1] and v1 >= bot[3] for x, v0, v1 in V)
        if left and right:
            return True
    return False


def _stack_align(geos) -> str:
    starts = [g[0] for g in geos]
    centres = [g[0] + g[3] / 2 for g in geos]
    return "centre" if (max(centres) - min(centres)) < (max(starts) - min(starts)) else "start"


def is_prose(text: str) -> bool:
    words = [w for w in text.replace("'", " ").split() if sum(c.isalpha() for c in w) >= 3]
    return len(words) >= 1


FUNCTION_TAIL = {"de", "du", "des", "la", "le", "les", "et", "à", "a", "en", "au", "aux", "par", "pour",
                 "sur", "dans", "avec", "of", "the", "and", "to", "in", "for", "with", "on", "by"}


def continues(prev: str, nxt: str) -> bool:
    """Next line continues the sentence of the previous one."""
    p, n = prev.rstrip(), nxt.lstrip(" .")
    if not p or not n or n[0] in "-–•*" or p.endswith((":", ".", ";")):
        return False
    if n[0].islower():
        return True
    last = p.split()[-1].casefold()
    return p.endswith((",", "-")) or last in FUNCTION_TAIL


def group_paragraphs(rows, V, Hs):
    """Join consecutive prose lines of one paragraph (same start, tight gap)."""
    by_rot: dict = {}
    for i, r in enumerate(rows):
        by_rot.setdefault(r["display_rotation"], []).append(i)
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for rot, idx in by_rot.items():
        idx = [i for i in idx if is_prose(rows[i]["source_text"])]
        idx.sort(key=lambda i: _geo(rows[i]["display_box"], rot)[1])
        for n, i in enumerate(idx):
            si, ci, ti, li = _geo(rows[i]["display_box"], rot)
            for j in idx[n + 1:]:
                sj, cj, tj, lj = _geo(rows[j]["display_box"], rot)
                gap = cj - (ci + ti)
                if gap > 1.0 * max(ti, tj):
                    break
                nxt = rows[j]["source_text"]
                lower_next = nxt.lstrip(" .")[:1].islower() and any(
                    len(w) >= 3 and _known(w) for w in re.findall(r"[^\W\d_]+", nxt))
                lo_r, hi_r = (0.6, 1.7) if lower_next else (0.75, 1.33)  # glyph boxes include descenders
                if gap < -0.3 * ti or not (lo_r <= tj / ti <= hi_r):
                    continue
                # left-aligned or centred stacks ('Salle de' / 'repos')
                # a line that plainly continues the sentence may sit a little off
                tol = max((1.0 if lower_next else 0.6) * ti, 6)
                same_start = abs(si - sj) <= tol
                same_centre = abs((si + li / 2) - (sj + lj / 2)) <= tol
                if not (same_start or same_centre) or rows[i]["origin"] != rows[j]["origin"]:
                    continue
                # a full-width rule (table border) separates the lines -- except
                # when the next line plainly runs on in lower case: on plans,
                # walls and slab edges often pass between the two lines of a label
                if not lower_next and _rule_between(rows[i]["display_box"], rows[j]["display_box"], rot, V, Hs):
                    continue
                if not continues(rows[i]["source_text"], rows[j]["source_text"]):
                    continue
                # only chain the line directly below (first match)
                if find(j) == j and sum(find(k) == find(i) for k in idx) >= 1:
                    parent[j] = find(i)
                    break
    groups: dict = {}
    for i in range(len(rows)):
        groups.setdefault(find(i), []).append(i)
    out = []
    for g in groups.values():
        if len(g) == 1:
            out.append(rows[g[0]])
            continue
        rot = rows[g[0]]["display_rotation"]
        g.sort(key=lambda i: _geo(rows[i]["display_box"], rot)[1])
        mem = [rows[i] for i in g]
        boxes = [m["display_box"] for m in mem]
        scores = [m["ocr_score"] for m in mem if m.get("ocr_score") is not None]
        out.append({**mem[0],
                    "source_text": " ".join(m["source_text"] for m in mem),
                    "display_box": [min(b[0] for b in boxes), min(b[1] for b in boxes),
                                    max(b[2] for b in boxes), max(b[3] for b in boxes)],
                    "line_boxes": boxes, "line_texts": [m["source_text"] for m in mem],
                    "line_align": _stack_align([_geo(b, rot) for b in boxes]),
                    "ocr_score": (sum(scores) / len(scores)) if scores else None,
                    "words": [w for m in mem for w in (m.get("words") or [])] or None, "source_passes": sorted({p for m in mem for p in m["source_passes"]})})
    return out


# ------------------------------------------------------------- missed lines

def _small_paths(page: fitz.Page, dpi: float):
    """Every drawing path with its display-px box (for glyph-level re-reading)."""
    k = dpi / 72.0
    rm = page.rotation_matrix
    out = []
    for d in page.get_drawings():
        r = d["rect"] * rm
        out.append((fitz.Rect(r.x0 * k, r.y0 * k, r.x1 * k, r.y1 * k), d))
    return out


def _render_paths(page: fitz.Page, drawings, clip_page: fitz.Rect, zoom: float) -> Image.Image:
    """Render only the given paths, in black, on an empty page of the same size."""
    doc = fitz.open()
    tp = doc.new_page(width=page.rect.width, height=page.rect.height)
    if page.rotation:
        tp.set_rotation(page.rotation)
    for d in drawings:
        sh = tp.new_shape()
        for it in d["items"]:
            if it[0] == "l":
                sh.draw_line(it[1], it[2])
            elif it[0] == "c":
                sh.draw_bezier(it[1], it[2], it[3], it[4])
            elif it[0] == "re":
                sh.draw_rect(it[1])
            elif it[0] == "qu":
                sh.draw_quad(it[1])
        filled = d.get("fill") is not None
        sh.finish(color=(0, 0, 0) if d.get("color") is not None else None, fill=(0, 0, 0) if filled else None,
                  width=max(0.3, float(d.get("width") or 0.3)), closePath=bool(d.get("closePath")),
                  even_odd=bool(d.get("even_odd")))
        sh.commit()
    pix = tp.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip_page, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
    doc.close()
    return img


def _solid_block(path, t) -> bool:
    """Filled squares (column marks, hatching blocks) are not letters."""
    r, d = path
    return (d.get("fill") is not None and len(d["items"]) <= 2 and all(it[0] in ("re", "qu") for it in d["items"])
            and min(r.width, r.height) > 0.35 * t)


def _main_run(glyphs, lo, hi, t):
    """Keep the run of strokes that sits under/over the label, not distant marks."""
    if not glyphs:
        return glyphs
    glyphs = sorted(glyphs, key=lambda g: g[0].x0)
    runs, cur, right = [], [glyphs[0]], glyphs[0][0].x1
    for g in glyphs[1:]:
        if g[0].x0 - right > 1.2 * t:
            runs.append(cur)
            cur = []
        cur.append(g)
        right = max(right, g[0].x1)
    runs.append(cur)
    keep = [g for run in runs
            if min(hi, max(x[0].x1 for x in run)) - max(lo, min(x[0].x0 for x in run)) > 0 for g in run]
    return keep


_LEX = None


def _lexicon():
    global _LEX
    if _LEX is None:
        from .classify import lexicon
        _LEX = lexicon("fr") | lexicon("en")  # plausibility only, never used to rewrite words
    return _LEX


def _good_read(r) -> bool:
    """A read with at least one real source-language word of 4+ letters."""
    if r.get("origin") == "native":
        return True
    if (r.get("ocr_score") or 0) < 0.6:
        return False
    from .classify import lexicon, strip_accents
    fr = lexicon("fr")
    return any(len(w) >= 4 and strip_accents(w.casefold()) in fr for w in re.findall(r"[^\W\d_]+", r["source_text"]))


def _known(w: str) -> bool:
    from .classify import strip_accents
    return strip_accents(re.sub(r"[^\w]", "", w).casefold()) in _lexicon()


def reocr_neighbours(page: fitz.Page, rows: list[dict], dpi: float, langs: str,
                     exe: str = "tesseract") -> list[dict]:
    """Second look at the line just below/above each recognised label.

    A label's second line is often missed because a wall or dimension line
    runs through it.  If glyph-sized strokes sit there and no row covers them,
    those strokes alone (long lines left out) are rendered at double
    resolution and read as one line.  Only readable words are kept.
    """
    paths = _small_paths(page, dpi)
    grid: dict = {}
    G = 200
    for i, (r, _d) in enumerate(paths):
        for gx in range(int(r.x0) // G, int(r.x1) // G + 1):
            for gy in range(int(r.y0) // G, int(r.y1) // G + 1):
                grid.setdefault((gx, gy), []).append(i)
    def solid(r):  # reads that count as text; weak junk reads must not hide a line
        return r.get("origin") == "native" or ((r.get("ocr_score") or 0) >= 0.6 and any(
            sum(c.isalnum() for c in w) >= 3 for w in r["source_text"].split()))
    boxes = [fitz.Rect(r["display_box"]) for r in rows if solid(r)]
    found: list[dict] = []
    seen_bands: list[fitz.Rect] = []
    for r in rows:
        if r.get("origin") != "ocr" or r["display_rotation"] != 0 or (r.get("ocr_score") or 0) < 0.6:
            continue
        if not any(sum(c.isalpha() for c in w) >= 3 for w in r["source_text"].split()):
            continue
        x0, y0, x1, y1 = r["display_box"]
        t = y1 - y0
        ln = x1 - x0
        for sign in (1, -1):
            if sign > 0:   # the line below, room for descenders
                band = fitz.Rect(x0 - 0.2 * ln, y1 + 0.05 * t, x1 + 0.2 * ln, y1 + 1.7 * t)
            else:
                band = fitz.Rect(x0 - 0.2 * ln, y0 - 1.7 * t, x1 + 0.2 * ln, y0 - 0.05 * t)
            if any(band.intersects(sb) and (band & sb).get_area() > 0.5 * band.get_area() for sb in seen_bands):
                continue
            cand = set()
            for gx in range(int(band.x0) // G, int(band.x1) // G + 1):
                for gy in range(int(band.y0) // G, int(band.y1) // G + 1):
                    cand.update(grid.get((gx, gy), []))
            glyphs = [paths[i] for i in cand if band.contains(paths[i][0]) and max(paths[i][0].width, paths[i][0].height) <= 1.6 * t
                      and not _solid_block(paths[i], t)]
            glyphs = _main_run(glyphs, x0, x1, t)
            if len(glyphs) < 4:
                continue
            gb = fitz.Rect(glyphs[0][0])
            for g, _ in glyphs[1:]:
                gb |= g
            # a real reading already sits on these strokes: leave it alone
            if any(gb.intersects(fitz.Rect(o["display_box"])) and (gb & fitz.Rect(o["display_box"])).height >= 0.5 * gb.height
                   and _good_read(o) for o in rows):
                continue
            seen_bands.append(band)
            clip = fitz.Rect([v * 72 / dpi for v in (gb.x0 - 4, gb.y0 - 4, gb.x1 + 4, gb.y1 + 4)]) * page.derotation_matrix
            img = _render_paths(page, [d for _, d in glyphs], clip, 2 * dpi / 72)
            with tempfile.TemporaryDirectory() as td:
                f = Path(td) / "l.png"
                img.save(f)
                res = subprocess.run([exe, str(f), "stdout", "--psm", "7", "-l", langs, "tsv"], creationflags=_NO_WINDOW,
                                     capture_output=True, text=True)
            words = []
            for line in res.stdout.splitlines()[1:]:
                a = line.split("\t")
                if len(a) >= 12 and a[11].strip() and float(a[10]) >= 0:
                    words.append((a[11], float(a[10]) / 100))
            # stray marks around the line ('M', '=') are not part of it
            words = [(w, sc) for w, sc in words if sum(c.isalnum() for c in w) >= 2 or sc >= 0.5]
            while words and sum(c.isalnum() for c in words[0][0]) <= 1:
                words = words[1:]   # single marks at either end belong to the drawing
            while words and sum(c.isalnum() for c in words[-1][0]) <= 2 and not words[-1][0].isdigit():
                words = words[:-1]
            longw = [(w, sc) for w, sc in words if sum(c.isalpha() for c in w) >= 4]
            if not longw:
                continue
            known = sum(_known(w) for w, _ in longw)
            score = sum(sc for _, sc in longw) / len(longw)
            junk = sum(1 for w, _ in words if not _known(w) and sum(c.isalpha() for c in w) >= 3)
            if known * 2 < len(longw) or score < 0.45 or junk > known:
                continue
            text = norm_text(" ".join(w for w, _ in words))
            nb = [round(v, 1) for v in (gb.x0, gb.y0, gb.x1, gb.y1)]
            found.append({"origin": "ocr", "source_text": clean_leaders(text), "display_box": nb, "display_rotation": 0,
                          "words": None, "ocr_score": score, "source_passes": ["neighbour_reocr"],
                          "reocr_of": r["source_text"]})
            boxes.append(fitz.Rect(nb))
    return found


# ------------------------------------------------------------- main

def run(source: Path, out_dir: Path, dpi: float, langs: str, rotations, workers: int,
        tesseract: str | None = "tesseract") -> dict:
    """Rows for page 1 of ``source``.  Without ``tesseract`` only the native
    text layer is read (outline lettering is then counted, not translated)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(source)
    if len(doc) != 1:
        raise ValueError(f"single-page drawings only (got {len(doc)} pages)")
    page = doc[0]
    nat = native_rows(page, dpi)
    # OCR raster without the native text layer, so nothing is read twice
    tmp = fitz.open(source)
    tp = tmp[0]
    for r in nat:
        rr = fitz.Rect([v * 72 / dpi for v in r["display_box"]]) * tp.derotation_matrix
        tp.add_redact_annot(rr, fill=False)
    if nat:
        tp.apply_redactions(images=0, graphics=0, text=0)
    pix = tp.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False)
    gray = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
    tmp.close()
    raw = ocr_rows(gray, langs, rotations, workers, tesseract) if tesseract else []
    ocr, dropped = dedup(raw)
    V, Hs = rules(page, dpi)
    ocr, n_split = split_at_rules(ocr, V, Hs)
    extra = reocr_neighbours(page, nat + ocr, dpi, langs, tesseract) if tesseract else []
    if extra:
        # weak reads of the same strokes give way to the clean second look
        eb = [fitz.Rect(e["display_box"]) for e in extra]
        def covered(r):
            b = fitz.Rect(r["display_box"])
            return any(b.intersects(x) and (b & x).get_area() >= 0.5 * max(b.get_area(), 1) for x in eb)
        def partial(r):  # a junk fragment of the line that the second look read whole
            b = fitz.Rect(r["display_box"])
            return not _good_read(r) and any(b.intersects(x) and (b & x).get_area() >= 0.7 * max(b.get_area(), 1)
                                             and len(r["source_text"]) < len(e["source_text"]) for x, e in zip(eb, extra))
        ocr = [r for r in ocr if not ((((r.get("ocr_score") or 0) < 0.6 or not any(
            sum(c.isalpha() for c in w) >= 3 for w in r["source_text"].split())) and covered(r)) or partial(r))]
    ocr += extra
    rows = group_paragraphs(nat + ocr, V, Hs)
    rows.sort(key=lambda r: (r["display_box"][1], r["display_box"][0]))
    for i, r in enumerate(rows, 1):
        r["id"] = f"R{i:04d}"
    info = {
        "source": str(source), "source_sha256": sha256(source), "dpi": dpi, "langs": langs, "ocr": bool(tesseract),
        "page_rect": list(page.rect), "page_rotation": page.rotation,
        "display_size_px": list(gray.size),
        "counts": {"native_lines": len(nat), "ocr_raw": len(raw), "ocr_dedup_dropped": dropped,
                   "ocr_after_dedup": len(ocr), "split_at_rules": n_split, "neighbour_reocr_added": len(extra), "rows": len(rows),
                   "paragraphs": sum(1 for r in rows if r.get("line_boxes")),
                   "by_rotation": {str(k): sum(r["display_rotation"] == k for r in rows) for k in (0, 90, 180, 270)},
                   "vertical_rules": len(V), "horizontal_rules": len(Hs)},
        "rows": rows,
    }
    write_json(out_dir / "rows.json", info)
    return info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--dpi", type=float, default=200)
    ap.add_argument("--langs", default="fra+eng")
    ap.add_argument("--rotations", default="0,90,270")
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) - 2))
    a = ap.parse_args()
    info = run(a.source, a.out_dir, a.dpi, a.langs, tuple(int(x) for x in a.rotations.split(",")), a.workers)
    print(info["counts"])


if __name__ == "__main__":
    main()
