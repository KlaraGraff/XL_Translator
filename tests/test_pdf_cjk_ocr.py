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
