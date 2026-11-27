"""How a short label reads together with the text beside it on the page.

原位替换一行一行单独送去翻译，模型只看到「Xtra」这一个词，看不到上一行的
「the Firebeam」，就把产品名当普通英文译成「额外」。这里给每条短文字找出页面上紧挨着它的
几行，按阅读顺序连起来（「the Firebeam / 「Xtra」」），附在这一页的翻译要求后面，只供理解，
不另外翻译。和 CAD 给文档说明附「短词和相邻文字连起来的样子」是同一个意思。
"""
from __future__ import annotations

from .common import norm_text
from .extract import _geo

SHORT_WORDS = 2
SHORT_CHARS = 24
MAX_LABELS = 40
MAX_NEIGHBOURS = 4
NEIGHBOUR_CHARS = 40

HEADING = ("本页上单独成条、离开上下文容易译错的短文字，和它在页面上紧挨着的文字连起来是这样"
           "（每行一条，「」里是要翻译的那条，「/」是换行；只用来理解意思，不要翻译这一段）：")


def is_short(text: str) -> bool:
    t = norm_text(text)
    return bool(t) and any(c.isalpha() for c in t) and len(t) <= SHORT_CHARS and len(t.split()) <= SHORT_WORDS


def _near(a, b) -> float | None:
    """Distance from label ``a`` to row ``b`` (in line heights), or None when too far."""
    sa, ca, ta, la = a
    sb, cb, tb, lb = b
    t = max(ta, tb, 1e-6)
    cross = abs((ca + ta / 2) - (cb + tb / 2))
    if cross > 1.6 * t:
        return None
    gap = max(sb - (sa + la), sa - (sb + lb), 0.0)
    if gap > 3 * t:
        return None
    return cross / t + gap / t


def _units(rows: list[dict]):
    """Every text line on the page: (row, rotation, box, text); a paragraph gives one per line."""
    for o in rows:
        if not o.get("display_box"):
            continue
        rot = o.get("display_rotation", 0)
        boxes, texts = o.get("line_boxes"), o.get("line_texts")
        if boxes and texts and len(boxes) == len(texts):
            for b, t in zip(boxes, texts):
                yield o, rot, b, t
        else:
            yield o, rot, o["display_box"], o["source_text"]


def _clip(text: str, limit: int = NEIGHBOUR_CHARS) -> str:
    text = norm_text(text)
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def neighbour_lines(rows: list[dict], labels: list[dict]) -> list[str]:
    """One line per distinct short label in ``labels``: it and its nearest lines, in reading order."""
    units = [u for u in _units(rows) if any(c.isalpha() for c in u[3])]
    out: list[str] = []
    seen: set[str] = set()
    for r in labels:
        key = r.get("model_input") or norm_text(r["source_text"])
        if key in seen or not r.get("display_box") or not is_short(r["source_text"]) or r.get("line_boxes"):
            continue
        rot = r.get("display_rotation", 0)
        g = _geo(r["display_box"], rot)
        near = []
        for o, orot, box, text in units:
            if o is r or orot != rot:
                continue
            go = _geo(box, rot)
            d = _near(g, go)
            if d is not None:
                near.append((d, go, text))
        if not near:
            continue
        seen.add(key)
        near.sort(key=lambda x: x[0])
        group = [(g, None)] + [(go, text) for _d, go, text in near[:MAX_NEIGHBOURS]]
        t = g[2] or 1.0
        group.sort(key=lambda x: (round((x[0][1] + x[0][2] / 2) / t), x[0][0]))
        out_line, last = "", None
        for go, text in group:
            line = round((go[1] + go[2] / 2) / t)
            piece = f"「{norm_text(r['source_text'])}」" if text is None else _clip(text)
            if out_line:
                out_line += " / " if line != last else " "
            out_line += piece
            last = line
        out.append(out_line)
        if len(out) >= MAX_LABELS:
            break
    return out


def neighbour_block(rows: list[dict], labels: list[dict]) -> str:
    lines = neighbour_lines(rows, labels)
    return (HEADING + "\n" + "\n".join(lines)) if lines else ""
