"""Read a drawing's outline lettering with the PP-OCR models (core.ppocr_engine).

The page raster is cut into overlapping tiles for detection only (small
lettering needs full resolution); boxes cut by a tile edge are joined across
tiles, and every line is then read once from the whole page.  Upright lines
are read as they are; a tall box is read three ways -- turned either way
(text written bottom-to-top or top-to-bottom) and as a column of upright
characters -- and the best-scored reading wins.

The rows have the same shape as Tesseract's (``extract.ocr_rows``): text,
display box, reading direction, per-word boxes and scores.  Chinese and
Japanese characters are one "word" each, with ``sp`` marking a space before a
word, so lines split at a table rule keep their original spacing.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable

import numpy as np

from core.ppocr_engine import PPOCR, crop_quad

from .common import join_words

TILE, STEP = 1024, 832
TALL = 1.5            # height / width from which a box is read as turned or as a column
_CJK = re.compile(r"[぀-ヿ㐀-鿿豈-﫿가-힯]")


def _norm(text: str) -> str:
    # full-width Latin letters, digits and signs as their ASCII forms; CJK punctuation kept
    out = []
    for ch in text:
        if "！" <= ch <= "～" and ch not in "，：；（）！？":
            ch = unicodedata.normalize("NFKC", ch)
        out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


class _Box:
    __slots__ = ("x0", "y0", "x1", "y1", "quad", "score", "clipped", "tall")

    def __init__(self, quad: np.ndarray, score: float, clipped: bool):
        self.quad = quad
        self.x0, self.y0 = float(quad[:, 0].min()), float(quad[:, 1].min())
        self.x1, self.y1 = float(quad[:, 0].max()), float(quad[:, 1].max())
        self.score = score
        self.clipped = clipped
        w = max(np.linalg.norm(quad[0] - quad[1]), 1.0)
        h = max(np.linalg.norm(quad[0] - quad[3]), 1.0)
        self.tall = h / w >= TALL

    @property
    def area(self) -> float:
        return max(1.0, (self.x1 - self.x0) * (self.y1 - self.y0))

    def inter(self, o: _Box) -> float:
        ix = max(0.0, min(self.x1, o.x1) - max(self.x0, o.x0))
        iy = max(0.0, min(self.y1, o.y1) - max(self.y0, o.y0))
        return ix * iy

    def cross_overlap(self, o: _Box) -> float:
        """Overlap across the reading direction, as a share of the thinner box."""
        if self.tall:
            lo, hi = max(self.x0, o.x0), min(self.x1, o.x1)
            thin = min(self.x1 - self.x0, o.x1 - o.x0)
        else:
            lo, hi = max(self.y0, o.y0), min(self.y1, o.y1)
            thin = min(self.y1 - self.y0, o.y1 - o.y0)
        return max(0.0, hi - lo) / max(thin, 1.0)


def _union(a: _Box, b: _Box) -> _Box:
    x0, y0, x1, y1 = min(a.x0, b.x0), min(a.y0, b.y0), max(a.x1, b.x1), max(a.y1, b.y1)
    quad = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], np.float32)
    u = _Box(quad, max(a.score, b.score), a.clipped and b.clipped)
    u.tall = a.tall
    return u


def merge_tiles(boxes: list[_Box]) -> list[_Box]:
    """One box per text line.  Boxes of the same reading direction are joined when
    they are the same line seen by two tiles (they mostly overlap), or pieces of a
    line cut by a tile edge (one of them touches the edge and they share the line's
    height)."""
    parent = list(range(len(boxes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    G = 256
    grid: dict[tuple[int, int], list[int]] = {}
    for i, b in enumerate(boxes):
        for gx in range(int(b.x0) // G, int(b.x1) // G + 1):
            for gy in range(int(b.y0) // G, int(b.y1) // G + 1):
                grid.setdefault((gx, gy), []).append(i)
    seen: set[tuple[int, int]] = set()
    for members in grid.values():
        for n, i in enumerate(members):
            for j in members[n + 1:]:
                if (i, j) in seen:
                    continue
                seen.add((i, j))
                a, b = boxes[i], boxes[j]
                if a.tall != b.tall:
                    continue
                inter = a.inter(b)
                if inter <= 0:
                    continue
                same = inter >= 0.7 * min(a.area, b.area)
                cut = (a.clipped or b.clipped) and a.cross_overlap(b) >= 0.6
                if same or cut:
                    parent[find(i)] = find(j)
    groups: dict[int, list[_Box]] = {}
    for i, b in enumerate(boxes):
        groups.setdefault(find(i), []).append(b)
    out = []
    for members in groups.values():
        box = members[0]
        for other in members[1:]:
            box = _union(box, other)
        out.append(box)
    return out


def _column_strip(crop: np.ndarray) -> np.ndarray:
    """A column of upright characters laid out side by side as one line."""
    h, w = crop.shape
    n = max(1, int(round(h / max(w, 1))))
    step = h / n
    cells = [crop[int(i * step):int((i + 1) * step), :] for i in range(n)]
    side = max(c.shape[0] for c in cells)
    cells = [np.pad(c, ((0, side - c.shape[0]), (0, 0)), constant_values=255) for c in cells]
    return np.concatenate(cells, axis=1)


def _words(read, box: _Box, mode: str, crop_len: float) -> list[dict]:
    """Per-word boxes in page px from the character positions of one reading."""
    words: list[dict] = []
    cur: dict | None = None
    space = False
    for ch, p, a, b in read.chars:
        if ch.isspace():
            space = True
            cur = None
            continue
        cjk = bool(_CJK.match(ch))
        if cur is None or cjk or cur["cjk"]:
            cur = {"text": ch, "probs": [p], "a": a, "b": b, "cjk": cjk, "sp": space and bool(words)}
            words.append(cur)
        else:
            cur["text"] += ch
            cur["probs"].append(p)
            cur["b"] = b
        space = False
    # hatching or ticks read onto the end of a word ('Caniveau////') become a word of their own
    split = []
    for w in words:
        m = re.search(r"[/|\\]{2,}$", w["text"]) if not w["cjk"] else None
        if m and m.start() > 0:
            k = m.start()
            span = (w["b"] - w["a"]) / max(1, len(w["text"]))
            head = {**w, "text": w["text"][:k], "probs": w["probs"][:k], "b": w["a"] + span * k}
            tail = {**w, "text": w["text"][k:], "probs": w["probs"][k:], "a": w["a"] + span * k, "sp": False}
            split += [head, tail]
        else:
            split.append(w)
    words = split
    out = []
    for w in words:
        a, b = max(0.0, w["a"]), min(crop_len, w["b"])
        if mode == "h":
            bx = [box.x0 + a, box.y0, box.x0 + b, box.y1]
        elif mode == "up":       # bottom-to-top: reading starts at the bottom
            bx = [box.x0, box.y1 - b, box.x1, box.y1 - a]
        elif mode == "down":     # top-to-bottom, glyphs turned clockwise
            bx = [box.x0, box.y0 + a, box.x1, box.y0 + b]
        else:                    # column of upright characters: the strip is cells side by side
            cell = box.x1 - box.x0
            bx = [box.x0, box.y0 + a / max(cell, 1) * cell, box.x1, box.y0 + b / max(cell, 1) * cell]
        out.append({"text": _norm(w["text"]), "score": min(w["probs"]), "box": [round(v, 1) for v in bx],
                    "sp": w["sp"]})
    return [w for w in out if w["text"]]


INK = 160             # gray level counted as lettering ink when tightening boxes


def _ink(g: np.ndarray, box: _Box, thick: float, vertical: bool, level: int = INK) -> np.ndarray:
    """Ink mask of the box region without the lines running through it.

    A detection box is about a third of a character larger than the letters on
    every side; the tight box comes from the ink.  Strokes longer than a
    character -- rules, dimension and leader lines crossing the label -- are
    taken out first so they do not stretch it.
    """
    from core.ppocr_engine import _cv2

    cv2 = _cv2()
    x0, y0 = int(max(0, box.x0)), int(max(0, box.y0))
    x1, y1 = int(min(g.shape[1], box.x1 + 1)), int(min(g.shape[0], box.y1 + 1))
    mask = (g[y0:y1, x0:x1] < level).astype(np.uint8)
    if mask.size == 0:
        return mask
    em = thick / 1.35                     # detection boxes are ~1.35 character heights thick
    along, across = max(3, int(1.6 * em)), max(3, int(1.15 * em))
    kh, kv = (across, along) if vertical else (along, across)
    long_h = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((1, kh), np.uint8))
    long_v = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((kv, 1), np.uint8))
    return mask & ~(long_h | long_v) & 1


def _grow(proj: np.ndarray, lo: int, hi: int, gap: int) -> tuple[int, int]:
    """Widen [lo, hi) over ink that carries on with no blank run longer than ``gap``."""
    n = len(proj)
    lo, hi = max(0, min(n, lo)), max(0, min(n, hi))
    i, blank = lo - 1, 0
    while i >= 0 and blank <= gap:
        if proj[i]:
            lo, blank = i, 0
        else:
            blank += 1
        i -= 1
    i, blank = hi, 0
    while i < n and blank <= gap:
        if proj[i]:
            hi, blank = i + 1, 0
        else:
            blank += 1
        i += 1
    return lo, hi


# light grey lettering (annotations drawn in grey, ~160 on white) is ink too when
# black ink alone does not cover the letters that were read
_INK_LEVELS = (INK, 215)


def _tighten(g: np.ndarray, box: _Box, words: list[dict], vertical: bool) -> list[float]:
    """Shrink the row box and the word boxes onto the letters; returns the row box.

    Along the line the box runs from the first to the last character read and on
    over ink that continues without a gap, so a mark set apart from the label (and
    dropped by ``trim_marks``) stays out of the box and is not erased with it.  When
    the ink found does not span the characters that were read (grey lettering, a
    label on a dark fill), the detection box stays as it is."""
    thick = (box.x1 - box.x0) if vertical else (box.y1 - box.y0)
    full = [round(v, 1) for v in (box.x0, box.y0, box.x1, box.y1)]
    if not words:
        return full
    ox, oy = int(max(0, box.x0)), int(max(0, box.y0))
    em = thick / 1.35
    a, b = (1, 3) if vertical else (0, 2)
    lo = int(min(w["box"][a] for w in words) - (oy if vertical else ox))
    hi = int(max(w["box"][b] for w in words) - (oy if vertical else ox)) + 1
    for level in _INK_LEVELS:
        mask = _ink(g, box, thick, vertical, level)
        if not mask.any():
            continue
        proj = mask.any(axis=1) if vertical else mask.any(axis=0)
        start, stop = _grow(proj, lo, hi, max(2, int(0.6 * em)))
        if vertical:
            mask[:start, :] = 0
            mask[stop:, :] = 0
        else:
            mask[:, :start] = 0
            mask[:, stop:] = 0
        if not mask.any():
            continue
        ys, xs = np.nonzero(mask)
        row = [float(xs.min() + ox), float(ys.min() + oy), float(xs.max() + 1 + ox), float(ys.max() + 1 + oy)]
        span = (row[3] - row[1]) if vertical else (row[2] - row[0])
        across = (row[2] - row[0]) if vertical else (row[3] - row[1])
        if span >= 0.6 * (hi - lo) and across >= 0.45 * em:
            break
    else:
        return full
    for w in words:
        wb = list(w["box"])
        if vertical:
            sub = mask[int(max(0, wb[1] - oy)):int(max(0, wb[3] - oy)), :]
            idx = np.nonzero(sub.any(axis=0))[0]
            wb[0], wb[2] = (float(idx.min() + ox), float(idx.max() + 1 + ox)) if idx.size else (row[0], row[2])
            wb[1], wb[3] = max(wb[1], row[1]), min(wb[3], row[3])
        else:
            sub = mask[:, int(max(0, wb[0] - ox)):int(max(0, wb[2] - ox))]
            idx = np.nonzero(sub.any(axis=1))[0]
            wb[1], wb[3] = (float(idx.min() + oy), float(idx.max() + 1 + oy)) if idx.size else (row[1], row[3])
            wb[0], wb[2] = max(wb[0], row[0]), min(wb[2], row[2])
        if wb[2] <= wb[0] or wb[3] <= wb[1]:
            continue                      # a word outside the ink keeps its reading box
        w["box"] = [round(v, 1) for v in wb]
    return [round(v, 1) for v in row]


# Small drawing marks next to a label that detection takes into the line: a circle
# read as 「。」/ o, a square as 口, a tick as 丨 or /, hatching as a run of slashes.
_MARKS = set("。○◯°·•口囗□△▽丨|/\\-—_~、,.oO")
_CJK_MARKS = set("。口囗丨、")     # real characters too: only dropped when set apart
_HATCH = re.compile(r"[/|\\]{2,}")


def _is_mark(w: dict) -> bool:
    return bool(w["text"]) and all(c in _MARKS for c in w["text"])


def trim_marks(words: list[dict]) -> list[dict]:
    """Drop marks at either end of a line: set apart by a space, hatching read as
    slashes, a Latin mark read with little certainty, or a 「。」 closing a short
    label (a sentence keeps its full stop)."""
    cjk = sum(1 for w in words for c in w["text"] if _CJK.match(c))

    def drop(w: dict, gap: bool) -> bool:
        if not _is_mark(w):
            return False
        if gap or _HATCH.fullmatch(w["text"]):
            return True
        if w["text"] == "。":
            return cjk < 8
        return w["text"] not in _CJK_MARKS and w["score"] < 0.6

    while len(words) > 1 and drop(words[-1], words[-1].get("sp", False)):
        words = words[:-1]
    while len(words) > 1 and drop(words[0], words[1].get("sp", False)):
        words = words[1:]
        words[0] = {**words[0], "sp": False}
    return words


def ppocr_rows(gray, engine: PPOCR, progress: Callable[[int, int], None] | None = None) -> list[dict]:
    """Rows of ``gray`` (PIL image, mode L); ``progress(done, total)`` follows the tiles."""
    g = np.asarray(gray, dtype=np.uint8)
    H, W = g.shape
    xs = list(range(0, max(1, W - (TILE - STEP)), STEP)) or [0]
    ys = list(range(0, max(1, H - (TILE - STEP)), STEP)) or [0]
    tiles = [(x, y) for y in ys for x in xs]
    total = len(tiles) + 1
    if progress:
        progress(0, total)
    boxes: list[_Box] = []
    for n, (x, y) in enumerate(tiles, 1):
        sub = g[y:y + TILE, x:x + TILE]
        th, tw = sub.shape
        for quad, score in engine.detect(sub):
            quad = quad + np.array([x, y], np.float32)
            m = 3
            clipped = ((quad[:, 0].min() <= x + m and x > 0) or (quad[:, 1].min() <= y + m and y > 0)
                       or (quad[:, 0].max() >= x + tw - 1 - m and x + tw < W)
                       or (quad[:, 1].max() >= y + th - 1 - m and y + th < H))
            boxes.append(_Box(quad, score, bool(clipped)))
        if progress:
            progress(n, total)
    boxes = merge_tiles(boxes)
    crops, plan = [], []
    for i, b in enumerate(boxes):
        c = crop_quad(g, b.quad)
        if not b.tall:
            plan.append((i, "h", len(crops), c.shape[1]))
            crops.append(c)
            continue
        plan.append((i, "up", len(crops), c.shape[0]))
        crops.append(np.ascontiguousarray(np.rot90(c, k=-1)))
        plan.append((i, "down", len(crops), c.shape[0]))
        crops.append(np.ascontiguousarray(np.rot90(c, k=1)))
        strip = _column_strip(c)
        plan.append((i, "col", len(crops), strip.shape[1]))
        crops.append(strip)
    reads = engine.recognize(crops)
    best: dict[int, tuple] = {}
    for i, mode, k, length in plan:
        r = reads[k]
        if not r.text.strip():
            continue
        # upright columns are a Chinese / Japanese way of writing: Latin letters read
        # one under the other come out as confident junk ('MCF 1H' -> 'HFM')
        if mode == "col" and 2 * sum(1 for c in r.text if _CJK.match(c)) < len(r.text.replace(" ", "")):
            continue
        # a reading must have more than one character to beat another direction on score alone
        key = (r.score * min(1.0, 0.5 + 0.25 * len(r.text.strip())), r.score)
        if i not in best or key > best[i][0]:
            best[i] = (key, mode, r, length)
    rows = []
    for i, (_key, mode, r, length) in best.items():
        b = boxes[i]
        words = trim_marks(_words(r, b, mode, length))
        if not words:
            continue
        text = _norm(join_words(words))
        box = _tighten(g, b, words, vertical=mode != "h")
        # a column of upright characters is written back turned (bottom to top) like
        # other vertical labels; its words are dropped so nothing re-joins them backwards
        rot = {"h": 0, "up": 90, "down": 270, "col": 90}[mode]
        rows.append({
            "origin": "ocr", "source_text": text, "display_box": box, "display_rotation": rot,
            "words": None if mode == "col" else words, "ocr_score": float(r.score),
            "source_passes": [f"ppocr_{mode}"], "clipped": False, "engine": "ppocr",
            **({"vertical_column": True} if mode == "col" else {}),
        })
    if progress:
        progress(total, total)
    return rows
