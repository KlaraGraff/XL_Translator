"""段落重排（BabelDOC）译出的每一页，先过一遍我们自己的检查，不过的页退回别的方式。

BabelDOC 没有自检：模型改了数字、把清单并成一段、字缩到看不清，它都照样交出来，用户
看不出来。这里逐页拿原文页和译文页比（2026-11-27 Mac 上用 4 份样本实测过的规则，见
scripts/gate.py 的来历，阈值没改）：

- 数字：原文和译文的数字字符逐个数，少了或多了 2 个及以上就挡（ICAO 页脚 14/6/24 被改成
  「2024年6月14日」、示例数据被「本地化」，都是这样挡下的）；
- 缩字：译文里字号小于 max(5pt, 全文正文字号 × 0.6) 的字比原文这一页多出 3% 以上；
- 残留标记：``<style id='1'>``、``{v1}`` 这类排版标记漏进了正文（原文本来就有的不算）；
- 列表合并：原文行首有 3 个以上列表标记，译文剩下不到 60%；
- 文字重叠：译文行框互相压住 35% 以上的比原文多出 3 处及以上；
- 漏翻：原文里成句的行（5 个以上单词，或 8 个以上汉字）原样出现在译文页上，2 行及以上，
  或占这一页成句行的 20% 以上；
- 译文偏少（只看中日韩目标语言）：这一页每个原文单词对应的译文字数，低于全文中位数的 45%。

只做判断，不改文件。用「PDF 处理组件」里的 PyMuPDF 读页面。
"""
from __future__ import annotations

import collections
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

REASON_MISSING_PAGE = "这一页没有排出来"
REASON_NUMBERS = "数字和原文对不上"
REASON_TINY = "字缩得太小"
REASON_MARKUP = "正文里留下了排版标记"
REASON_LIST = "列表被并成了一段"
REASON_OVERLAP = "文字叠在一起"
REASON_UNTRANSLATED = "有段落没有翻译"
REASON_SHORT = "译文明显比原文少"

_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]")
_WORD = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿĀ-ſА-я]{2,}")
_MARKUP = re.compile(r"<\s*/?\s*(?:style|b\d*|i|span|font|sup|sub)\b[^>]*>|\{\s*v\s*\d+\s*\}|<\|", re.I)
_LIST_MARK = re.compile(r"^\s*(?:[-•–▪◦●]\s|\(?[ivx]{1,4}\)\s|\(?\d{1,2}[.)]\s|\([a-h]\)\s)")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200d\u2060\ufeff]")
_SPACE = re.compile(r"\s+")

_CJK_TARGETS = ("zh", "ja", "ko")
_DIGIT_TOLERANCE = 2
_TINY_SHARE = 0.03
_TINY_FLOOR_PT = 5.0
_TINY_FACTOR = 0.6
_LIST_MIN = 3
_LIST_KEEP = 0.6
_OVERLAP_SHARE = 0.35
_OVERLAP_EXTRA = 3
_SENTENCE_WORDS = 5
_SENTENCE_CJK = 8
_UNTRANSLATED_LINES = 2
_UNTRANSLATED_SHARE = 0.2
_SHORT_MIN_WORDS = 40
_SHORT_FACTOR = 0.45


@dataclass
class PageVerdict:
    page_number: int
    reasons: list[str] = field(default_factory=list)
    #: 写进诊断日志的细节（少了哪些数字、最小字号……），不给用户看
    details: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.reasons

    def add(self, reason: str, detail: str = "") -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)
        if detail:
            self.details.append(detail)


@dataclass
class _Line:
    text: str
    sizes: list[tuple[float, int]]  # (字号, 这个字号的字数)
    rect: tuple[float, float, float, float]


def _lines(page) -> list[_Line]:
    out: list[_Line] = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(str(span.get("text") or "") for span in spans)
            if not text.strip():
                continue
            sizes = [(float(span.get("size") or 0.0), _letters(str(span.get("text") or ""))) for span in spans]
            x0, y0, x1, y1 = (float(value) for value in (line.get("bbox") or (0, 0, 0, 0)))
            out.append(_Line(_ZERO_WIDTH.sub("", text), sizes, (x0, y0, x1, y1)))
    return out


def _letters(text: str) -> int:
    return sum(1 for ch in text if ch.isalpha())


def _digits(text: str) -> collections.Counter:
    return collections.Counter(ch for ch in text if ch.isdigit())


def _tiny_share(lines: list[_Line], limit: float) -> tuple[float, float]:
    """(小于 limit 的字占的比例, 最小字号)。"""
    total = tiny = 0
    smallest = 0.0
    for line in lines:
        for size, count in line.sizes:
            if not count:
                continue
            total += count
            if size < limit:
                tiny += count
            smallest = size if not smallest else min(smallest, size)
    return (tiny / total if total else 0.0), smallest


def _overlaps(lines: list[_Line]) -> int:
    rects = [line.rect for line in lines
             if len(line.text.strip()) > 2 and line.rect[2] - line.rect[0] > 5 and line.rect[3] - line.rect[1] > 3]
    count = 0
    for index, first in enumerate(rects):
        area_a = (first[2] - first[0]) * (first[3] - first[1])
        for second in rects[index + 1:]:
            width = min(first[2], second[2]) - max(first[0], second[0])
            height = min(first[3], second[3]) - max(first[1], second[1])
            if width <= 0 or height <= 0:
                continue
            area_b = (second[2] - second[0]) * (second[3] - second[1])
            if width * height > _OVERLAP_SHARE * min(area_a, area_b):
                count += 1
    return count


def _normalized(text: str) -> str:
    return _SPACE.sub(" ", _ZERO_WIDTH.sub("", text)).strip().casefold()


def _sentence_like(text: str) -> bool:
    return len(_WORD.findall(text)) >= _SENTENCE_WORDS or len(_CJK.findall(text)) >= _SENTENCE_CJK


def _untranslated(source_lines: list[_Line], output_text: str) -> tuple[int, int, str]:
    """(原样留在译文里的成句行数, 这一页成句行数, 第一行的开头)。"""
    haystack = _normalized(output_text)
    kept = total = 0
    sample = ""
    for line in source_lines:
        if not _sentence_like(line.text):
            continue
        total += 1
        needle = _normalized(line.text)
        if needle and needle in haystack:
            kept += 1
            sample = sample or needle[:40]
    return kept, total, sample


def check_pages(source_pdf: Path, output_pdf: Path, pages: list[int], *, target_lang: str) -> dict[int, PageVerdict]:
    """逐页比对 ``pages``（从 1 数）；两份 PDF 的页序相同（BabelDOC 不加页、不删页）。"""
    from core.pdf_component import require_fitz

    fitz = require_fitz()
    cjk_target = str(target_lang or "").lower().split("-", 1)[0] in _CJK_TARGETS
    verdicts = {number: PageVerdict(number) for number in pages}
    with fitz.open(str(source_pdf)) as source, fitz.open(str(output_pdf)) as output:
        wanted = [number for number in sorted(verdicts) if 0 < number <= source.page_count]
        source_lines = {number: _lines(source[number - 1]) for number in wanted}
        # 全文正文字号：原文每个字母按它的字号计，取中位数。
        sizes = [size for lines in source_lines.values() for line in lines for size, count in line.sizes
                 for _ in range(min(count, 50))]
        base = statistics.median(sizes) if sizes else 10.0
        tiny_limit = max(_TINY_FLOOR_PT, _TINY_FACTOR * base)
        # 「译文偏少」比的是全文的中位数：先把每页的比例算出来。
        ratios: dict[int, tuple[int, int]] = {}
        output_texts: dict[int, str] = {}
        output_lines: dict[int, list[_Line]] = {}
        for number in wanted:
            if number > output.page_count:
                verdicts[number].add(REASON_MISSING_PAGE, f"output has {output.page_count} pages")
                continue
            page = output[number - 1]
            output_texts[number] = _ZERO_WIDTH.sub("", page.get_text())
            output_lines[number] = _lines(page)
            words = sum(len(_WORD.findall(line.text)) for line in source_lines[number])
            ratios[number] = (words, len(_CJK.findall(output_texts[number])))
        typical = [cjk / words for words, cjk in ratios.values() if words >= _SHORT_MIN_WORDS]
        median_ratio = statistics.median(typical) if typical else 0.0
        for number in wanted:
            if number not in output_texts:
                continue
            verdict = verdicts[number]
            src_text = _ZERO_WIDTH.sub("", source[number - 1].get_text())
            out_text = output_texts[number]
            lost = sum((_digits(src_text) - _digits(out_text)).values())
            added = sum((_digits(out_text) - _digits(src_text)).values())
            if lost >= _DIGIT_TOLERANCE or added >= _DIGIT_TOLERANCE:
                verdict.add(REASON_NUMBERS, f"digits lost {lost}, added {added}")
            src_tiny, _ = _tiny_share(source_lines[number], tiny_limit)
            out_tiny, smallest = _tiny_share(output_lines[number], tiny_limit)
            if out_tiny - src_tiny > _TINY_SHARE:
                verdict.add(REASON_TINY, f"{out_tiny:.0%} of letters below {tiny_limit:.1f}pt (smallest {smallest:.1f}pt)")
            marker = _MARKUP.search(out_text)
            if marker and not _MARKUP.search(src_text):
                verdict.add(REASON_MARKUP, f"leaked {marker.group(0)[:20]!r}")
            src_marks = sum(1 for line in source_lines[number] if _LIST_MARK.match(line.text))
            out_marks = sum(1 for line in output_lines[number] if _LIST_MARK.match(line.text))
            if src_marks >= _LIST_MIN and out_marks < _LIST_KEEP * src_marks:
                verdict.add(REASON_LIST, f"list markers {src_marks} -> {out_marks}")
            extra_overlaps = _overlaps(output_lines[number]) - _overlaps(source_lines[number])
            if extra_overlaps >= _OVERLAP_EXTRA:
                verdict.add(REASON_OVERLAP, f"{extra_overlaps} more overlapping lines than the source")
            kept, total, sample = _untranslated(source_lines[number], out_text)
            if kept >= _UNTRANSLATED_LINES or (kept and kept >= _UNTRANSLATED_SHARE * total):
                verdict.add(REASON_UNTRANSLATED, f"{kept}/{total} source lines kept, e.g. {sample!r}")
            words, cjk = ratios.get(number, (0, 0))
            if cjk_target and median_ratio and words >= _SHORT_MIN_WORDS and cjk / words < _SHORT_FACTOR * median_ratio:
                verdict.add(REASON_SHORT, f"{cjk} characters for {words} words (median {median_ratio:.2f})")
    for number, verdict in verdicts.items():
        if number not in output_texts and not verdict.reasons:
            verdict.add(REASON_MISSING_PAGE, "page number outside the source")
    return verdicts
