"""读中文、日文线条字（core.pdf_vector.cjk_ocr）：分块后的框怎么拼回一行、读法怎么变成词。

要 NumPy 和 OpenCV（随「PDF 处理组件」装，不在测试环境的必装依赖里）：没有时整份跳过。
"""
from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")

from core.pdf_vector import cjk_ocr  # noqa: E402
from core.ppocr_engine import LineRead  # noqa: E402


def _box(x0, y0, x1, y1, clipped=False):
    quad = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], np.float32)
    return cjk_ocr._Box(quad, 0.9, clipped)


def _rects(boxes):
    return sorted((round(b.x0), round(b.y0), round(b.x1), round(b.y1)) for b in boxes)


def test_a_line_cut_by_a_tile_edge_is_joined():
    # 第一块里只有左半截（碰到块的右边），第二块里是整行
    left = _box(780, 100, 1023, 130, clipped=True)
    whole = _box(780, 101, 1100, 131)
    other_line = _box(780, 200, 1100, 230)
    assert _rects(cjk_ocr.merge_tiles([left, whole, other_line])) == [(780, 100, 1100, 131), (780, 200, 1100, 230)]


def test_pieces_cut_by_an_edge_join_but_neighbouring_labels_do_not():
    a = _box(700, 100, 1023, 130, clipped=True)
    b = _box(1010, 102, 1200, 129, clipped=True)
    assert _rects(cjk_ocr.merge_tiles([a, b])) == [(700, 100, 1200, 130)]
    # 两个挨着的标注（都没碰到块边）各是各的
    c = _box(100, 100, 300, 130)
    d = _box(295, 100, 500, 130)
    assert len(cjk_ocr.merge_tiles([c, d])) == 2
    # 竖排的和横排的不拼
    tall = _box(990, 60, 1020, 400, clipped=True)
    assert len(cjk_ocr.merge_tiles([a, tall])) == 2


def _w(text, score=0.95, sp=False):
    return {"text": text, "score": score, "box": [0, 0, 1, 1], "sp": sp}


def test_marks_at_the_ends_of_a_label_are_dropped():
    def texts(words):
        return [w["text"] for w in cjk_ocr.trim_marks(words)]

    # 圆圈读成句号、隔着空格的小记号、读成斜线的填充线
    assert texts([_w("低"), _w("压"), _w("配"), _w("电"), _w("间"), _w("。")]) == ["低", "压", "配", "电", "间"]
    assert texts([_w("Salle"), _w("de", sp=True), _w("repos", sp=True), _w("o", sp=True)]) == ["Salle", "de", "repos"]
    assert texts([_w("Caniveau"), _w("////")]) == ["Caniveau"]
    # 句子末尾的句号、字里的「口」照旧保留
    sentence = [_w(c) for c in "所有管道穿墙处均设套管。"]
    assert texts(sentence)[-1] == "。"
    assert texts([_w("出"), _w("口")]) == ["出", "口"]
    # 拿不准的西文小记号去掉，读得准的不动
    assert texts([_w("A"), _w("-", score=0.3)]) == ["A"]
    assert texts([_w("A"), _w("-", score=0.9)]) == ["A", "-"]


def test_chinese_characters_are_words_of_their_own():
    read = LineRead("配电 TN3000////", 0.9, [
        ("配", 0.99, 0, 10), ("电", 0.98, 10, 20), (" ", 0.9, 20, 22),
        *[(c, 0.97, 22 + 5 * n, 27 + 5 * n) for n, c in enumerate("TN3000")],
        *[("/", 0.6, 52 + 2 * n, 54 + 2 * n) for n in range(4)],
    ])
    words = cjk_ocr._words(read, _box(100, 50, 160, 60), "h", 60)
    assert [w["text"] for w in words] == ["配", "电", "TN3000", "////"]
    assert [w["sp"] for w in words] == [False, False, True, False]
    assert words[0]["box"] == [100.0, 50.0, 110.0, 60.0]
    assert words[2]["score"] == 0.97 and words[3]["score"] == 0.6


def test_turned_lines_map_back_onto_the_page():
    read = LineRead("配电", 0.9, [("配", 0.99, 0, 10), ("电", 0.98, 10, 20)])
    box = _box(100, 50, 110, 70)
    up = cjk_ocr._words(read, box, "up", 20)        # read from the bottom up
    assert [w["box"] for w in up] == [[100.0, 60.0, 110.0, 70.0], [100.0, 50.0, 110.0, 60.0]]
    down = cjk_ocr._words(read, box, "down", 20)    # read from the top down
    assert [w["box"] for w in down] == [[100.0, 50.0, 110.0, 60.0], [100.0, 60.0, 110.0, 70.0]]


def test_full_width_letters_and_digits_become_ascii():
    assert cjk_ocr._norm("ＫＬ１（２）　配电") == "KL1（2） 配电"


def _page_with_ink(w=1000, h=900, level=0, rects=()):
    page = np.full((h, w), 255, np.uint8)
    for x0, y0, x1, y1 in rects:
        page[y0:y1, x0:x1] = level
    return page


def test_grey_lettering_still_gets_a_tight_box():
    # 字是浅灰色（约 160）：只按黑色找笔画时框会缩成一点，写回时译文没地方放
    letters = [(110 + 30 * n, 108, 130 + 30 * n, 128) for n in range(4)]
    for level in (0, 160):
        page = _page_with_ink(level=level, rects=letters)
        box = _box(100, 100, 230, 136)
        words = [{"text": c, "score": 0.99, "box": [110.0 + 30 * n, 100.0, 130.0 + 30 * n, 136.0], "sp": False}
                 for n, c in enumerate("配电间房")]
        row = cjk_ocr._tighten(page, box, words, vertical=False)
        assert row == [110.0, 108.0, 220.0, 128.0], (level, row)
        assert all(w["box"][0] < w["box"][2] and w["box"][1] < w["box"][3] for w in words)


def test_without_ink_under_the_reading_the_detection_box_stays():
    page = _page_with_ink(level=230, rects=[(110, 108, 220, 128)])   # too faint to count as ink
    box = _box(100, 100, 230, 136)
    words = [{"text": "配", "score": 0.99, "box": [110.0, 100.0, 140.0, 136.0], "sp": False}]
    assert cjk_ocr._tighten(page, box, words, vertical=False) == [100.0, 100.0, 230.0, 136.0]


class _FakeEngine:
    """整页一块（不大于 1024 像素）：检测出一个竖排的框，三种读法按 up、down、col 的顺序给出。"""

    def __init__(self, box, reads):
        self.box, self.reads = box, reads

    def detect(self, sub):
        return [(np.array([[self.box[0], self.box[1]], [self.box[2], self.box[1]], [self.box[2], self.box[3]],
                           [self.box[0], self.box[3]]], np.float32), 0.9)]

    def recognize(self, crops):
        return self.reads[:len(crops)]


def _read(text, score):
    step = 100 / max(1, len(text))
    return LineRead(text, score, [(c, score, n * step, (n + 1) * step) for n, c in enumerate(text)])


def test_latin_letters_are_not_read_as_an_upright_column():
    from PIL import Image

    page = _page_with_ink(rects=[(108, 105, 128, 195)])
    engine = _FakeEngine((100, 100, 136, 200), [_read("MCF 1H", 0.95), _read("MCF1H", 0.7), _read("HFM", 0.99)])
    rows = cjk_ocr.ppocr_rows(Image.fromarray(page), engine)
    assert [(r["source_text"], r["display_rotation"]) for r in rows] == [("MCF 1H", 90)]
    # 中文竖写（一个字一个字往下排）照旧按列读
    engine = _FakeEngine((100, 100, 136, 200), [_read("回电配", 0.7), _read("配电回", 0.6), _read("配电间", 0.98)])
    rows = cjk_ocr.ppocr_rows(Image.fromarray(page), engine)
    assert [(r["source_text"], r.get("vertical_column")) for r in rows] == [("配电间", True)]


def _horizontal(page_box, read):
    from PIL import Image

    engine = _FakeEngine(page_box, [read])
    return cjk_ocr.ppocr_rows(Image.fromarray(_page_with_ink()), engine)


def test_two_labels_read_as_one_line_are_split_at_the_wide_blank():
    # 「配电间」 and a code three character heights further on: two labels, two rows
    chars = [(c, 0.98, 20 * n, 20 * n + 18) for n, c in enumerate("配电间")]
    chars += [(c, 0.97, 140 + 10 * n, 148 + 10 * n) for n, c in enumerate("KL1")]
    rows = _horizontal((100, 100, 300, 127), LineRead("配电间KL1", 0.97, chars))
    assert [r["source_text"] for r in rows] == ["配电间", "KL1"]
    assert all(r.get("split_from_gap") for r in rows)
    first, second = (r["display_box"] for r in rows)
    assert first[2] < second[0]


def test_an_evenly_spaced_title_stays_one_row():
    # 「总 平 面 图」 letter-spaced: every blank is wide, none stands out
    chars = [(c, 0.98, 70 * n, 70 * n + 18) for n, c in enumerate("总平面图")]
    rows = _horizontal((100, 100, 400, 127), LineRead("总平面图", 0.98, chars))
    assert [r["source_text"] for r in rows] == ["总平面图"]
    # words with ordinary spaces stay together too
    words = "Salle de repos"
    chars = [(c, 0.95, 8 * n, 8 * n + 7) for n, c in enumerate(words)]
    rows = _horizontal((100, 100, 300, 127), LineRead(words, 0.95, chars))
    assert [r["source_text"] for r in rows] == ["Salle de repos"]


def test_fill_patterns_read_as_repeated_letters_are_not_rows():
    chars = [("m", 0.6, 9 * n, 9 * n + 8) for n in range(6)]
    assert _horizontal((100, 100, 300, 127), LineRead("mmmmmm", 0.6, chars)) == []
    assert cjk_ocr._hatch_read("mnmn mnmn") and not cjk_ocr._hatch_read("Mme") and not cjk_ocr._hatch_read("1111")


def _letters(x0, x1, y0=104, y1=122, step=14):
    """Letter-sized blocks of ink, so they are not taken for drawing lines."""
    return [(x, y0, x + 10, y1) for x in range(x0, x1 - 9, step)]


def test_two_cells_read_as_one_word_are_cut_at_the_wall_between_them():
    from PIL import Image

    # 'Area' | 'Circulating': a table wall runs across the line and on past it
    page = _page_with_ink(rects=[*_letters(102, 180), *_letters(212, 400), (199, 80, 201, 150)])
    chars = [(c, 0.95, 20 * n, 20 * n + 12) for n, c in enumerate("Area")]
    chars += [(c, 0.95, 110 + 18 * n, 122 + 18 * n) for n, c in enumerate("Circulating")]
    engine = _FakeEngine((100, 100, 410, 127), [LineRead("AreaCirculating", 0.95, chars)])
    rows = cjk_ocr.ppocr_rows(Image.fromarray(page), engine)
    assert [r["source_text"] for r in rows] == ["Area", "Circulating"]
    # a glyph's own stroke stays inside the line: no cut
    page = _page_with_ink(rects=[*_letters(102, 180), *_letters(212, 400), (199, 103, 201, 123)])
    rows = cjk_ocr.ppocr_rows(Image.fromarray(page), _FakeEngine((100, 100, 410, 127),
                                                                 [LineRead("AreaCirculating", 0.95, chars)]))
    assert [r["source_text"] for r in rows] == ["AreaCirculating"]


class _CountingEngine(_FakeEngine):
    def recognize(self, crops):
        self.widths = [c.shape[1] for c in crops]
        return super().recognize(crops)


def test_a_box_that_ends_inside_the_lettering_is_read_again_grown():
    from PIL import Image

    # the letters run from 110 to 400 but detection stopped at 300
    page = _page_with_ink(rects=_letters(110, 400))
    short = LineRead("Worksh", 0.93, [(c, 0.93, 30 * n, 30 * n + 20) for n, c in enumerate("Worksh")])
    whole = LineRead("Workshop", 0.95, [(c, 0.95, 36 * n, 36 * n + 24) for n, c in enumerate("Workshop")])
    engine = _CountingEngine((100, 100, 300, 127), [short, whole])
    rows = cjk_ocr.ppocr_rows(Image.fromarray(page), engine)
    assert [r["source_text"] for r in rows] == ["Workshop"]
    assert len(engine.widths) == 2 and engine.widths[1] > engine.widths[0]
    assert rows[0]["display_box"][2] >= 395
    # letters that stop inside the box: read once, as detected
    page = _page_with_ink(rects=_letters(110, 280))
    engine = _CountingEngine((100, 100, 300, 127), [short])
    rows = cjk_ocr.ppocr_rows(Image.fromarray(page), engine)
    assert len(engine.widths) == 1 and [r["source_text"] for r in rows] == ["Worksh"]
