"""Synthetic tests for core.pdf_vector (in-place PDF translation, Beta)."""
from __future__ import annotations


import pytest

from core.pdf_vector import classify as ct
from core.pdf_vector import extract
from core.pdf_vector.common import insert_rotate
from core.pdf_vector import find_cjk_font
from core.pdf_vector.writeback import FONT_NAME, apply_removal, place_line, render

# PyMuPDF 属于「PDF 处理组件」，不随安装包分发；没装它的环境跳过这些测试。
fitz = pytest.importorskip("fitz")

DPI = 72.0  # 1 px == 1 pt keeps the arithmetic readable


def ink_bbox(page):
    img = render(page, DPI).convert("L")
    px = img.load()
    xs, ys = [], []
    for y in range(img.height):
        for x in range(img.width):
            if px[x, y] < 128:
                xs.append(x)
                ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


@pytest.mark.parametrize("prot", [0, 90, 180, 270])
@pytest.mark.parametrize("drot", [0, 90, 180, 270])
def test_insert_rotate_reads_in_display_direction(prot, drot):
    d = fitz.open()
    p = d.new_page(width=400, height=300)
    p.set_rotation(prot)
    centre = fitz.Point(200, 150) * p.derotation_matrix if prot else fitz.Point(200, 150)
    cdisp = centre * p.rotation_matrix
    p.insert_text(centre, "HHHHHHHHHH", fontsize=12, rotate=insert_rotate(drot, prot))
    x0, y0, x1, y1 = ink_bbox(p)
    dx, dy = (x0 + x1) / 2 - cdisp.x, (y0 + y1) / 2 - cdisp.y
    want = {0: (1, 0), 90: (0, -1), 180: (-1, 0), 270: (0, 1)}[drot]
    got = (1 if dx > 10 else -1 if dx < -10 else 0, 1 if dy > 10 else -1 if dy < -10 else 0)
    assert got == want


def _glyph(page, rect):
    sh = page.new_shape()
    sh.draw_rect(rect)
    sh.finish(fill=(0, 0, 0), color=None)
    sh.commit()


def test_vector_removal_keeps_lines_crossing_text():
    d = fitz.open()
    p = d.new_page(width=300, height=200)
    for i in range(5):  # five "glyphs" of one OCR line
        _glyph(p, fitz.Rect(50 + i * 12, 50, 58 + i * 12, 62))
    p.draw_line((40, 56), (250, 56), color=(1, 0, 0), width=0.5)      # leader through the text
    p.draw_line((100, 20), (100, 150), color=(0, 0, 1), width=0.5)    # table rule next to it
    n0 = len(p.get_drawings())
    row = {"origin": "ocr", "display_box": [50, 50, 106, 62], "display_rotation": 0}
    apply_removal(p, [row], DPI)
    left = p.get_drawings()
    assert len(left) == n0 - 5
    assert {tuple(round(c, 1) for c in dr["color"]) for dr in left if dr.get("color")} == {(1.0, 0.0, 0.0), (0.0, 0.0, 1.0)}


def test_overlapping_rows_both_removed():
    """Adjacent lines whose padded rectangles overlap must both disappear."""
    d = fitz.open()
    p = d.new_page(width=300, height=200)
    for i in range(4):
        _glyph(p, fitz.Rect(50 + i * 12, 50, 58 + i * 12, 62))   # line 1
        _glyph(p, fitz.Rect(50 + i * 12, 64, 58 + i * 12, 76))   # line 2, 2pt below
    rows = [{"origin": "ocr", "display_box": [50, 50, 94, 62], "display_rotation": 0},
            {"origin": "ocr", "display_box": [50, 64, 94, 76], "display_rotation": 0}]
    apply_removal(p, rows, DPI)
    assert p.get_drawings() == []


def test_kept_row_glyphs_survive_straddler_cleanup():
    d = fitz.open()
    p = d.new_page(width=300, height=200)
    for i in range(4):
        _glyph(p, fitz.Rect(50 + i * 12, 50, 58 + i * 12, 62))
    _glyph(p, fitz.Rect(98, 50, 106, 62))  # neighbour label that stays
    row = {"origin": "ocr", "display_box": [50, 50, 96, 62], "display_rotation": 0}
    apply_removal(p, [row], DPI, protect=[[98, 50, 106, 62]])
    assert len(p.get_drawings()) == 1


def test_native_text_removed_graphics_untouched():
    d = fitz.open()
    p = d.new_page(width=300, height=200)
    p.insert_text((50, 60), "Alarme", fontsize=12)
    p.draw_line((40, 56), (250, 56), width=0.5)
    row = {"origin": "native", "display_box": [50, 50, 90, 62], "display_rotation": 0}
    apply_removal(p, [row], DPI)
    assert "Alarme" not in p.get_text()
    assert len(p.get_drawings()) == 1


@pytest.mark.parametrize("prot", [0, 90, 270])
def test_place_line_lands_in_target_band(prot):
    font_path = find_cjk_font()
    if font_path is None:
        pytest.skip("no CJK font on this machine")
    d = fitz.open()
    p = d.new_page(width=400, height=300)
    p.set_rotation(prot)
    p.insert_font(fontname=FONT_NAME, fontfile=str(font_path))
    font = fitz.Font(fontfile=str(font_path))
    rect = place_line(p, font, "基础底板表", 20, 0, 100, 100, DPI, (0, 0, 0))
    x0, y0, x1, y1 = ink_bbox(p)
    assert abs(x0 - 100) <= 3 and abs((y0 + y1) / 2 - 100) <= 3
    assert x1 <= rect[2] + 2


def lex_row(t, s=0.95, rot=0):
    return {"source_text": t, "origin": "ocr", "ocr_score": s, "display_rotation": rot}


def test_classification():
    assert ct.classify(lex_row("1.00"), 0.45)[1] == "retained_numeric"
    assert ct.classify(lex_row("1.00x1.30"), 0.45)[0] == "keep_source"
    assert ct.classify(lex_row("7T10"), 0.45)[1] == "retained_identifier"
    assert ct.classify(lex_row("Cad + Ep HA6", 0.93), 0.45)[1] == "retained_identifier"
    assert ct.classify(lex_row("eEeS"), 0.45)[0] == "review"
    assert ct.classify(lex_row("aoc", 0.85), 0.45)[0] == "review"
    assert ct.classify(lex_row("TABLEAU DES SEMELLES"), 0.45)[0] == "translate"
    assert ct.classify(lex_row("Fosse", 0.9, 90), 0.45)[0] == "translate"
    assert ct.classify(lex_row("Weds", 0.75, 90), 0.45)[0] == "review"
    assert ct.classify(lex_row("si hauteur bon sol/longrine h0 < 1.20"), 0.45)[1] == "ocr_operator_uncertain"


def test_clean_leaders():
    assert extract.clean_leaders("- Semelles : ........cccceeeee 5cm") == "- Semelles : ...... 5cm"
    assert extract.clean_leaders("fc28=25MPa") == "fc28=25MPa"


def _r(text, box, score, clipped=False):
    return {"source_text": text, "display_box": box, "display_rotation": 0, "ocr_score": score,
            "source_passes": ["x"], "clipped": clipped, "origin": "ocr"}


def test_dedup_full_line_beats_tile_fragments():
    rows = [_r("stes de Livraison", [7807, 5548, 8237, 5612], 0.93, clipped=True),
            _r("-BATI {l- Postes de L", [7516, 5549, 8040, 5612], 0.8, clipped=True),
            _r("-BATI 11- Postes de Livraison", [7515, 5548, 8237, 5612], 0.86)]
    kept, _ = extract.dedup(rows)
    assert [k["source_text"] for k in kept] == ["-BATI 11- Postes de Livraison"]


def test_dedup_keeps_separate_cells():
    rows = [_r("Type", [100, 100, 160, 120], 0.95), _r("Dimensions", [180, 100, 330, 120], 0.95)]
    kept, _ = extract.dedup(rows)
    assert len(kept) == 2


def test_split_at_table_rule():
    words = [{"text": "Type", "score": 0.9, "box": [100, 100, 160, 120]},
             {"text": "Dimensions", "score": 0.9, "box": [180, 100, 330, 120]}]
    row = {**_r("Type Dimensions", [100, 100, 330, 120], 0.9), "words": words}
    out, n = extract.split_at_rules([row], [(170, 80, 140)], [])
    assert n == 1 and [o["source_text"] for o in out] == ["Type", "Dimensions"]


def test_paragraph_only_joins_continuations():
    a = {**_r("Un plan de détection doit être élaboré et faire", [100, 100, 600, 120], 0.9)}
    b = {**_r("l'objet de validation.", [100, 124, 400, 144], 0.9)}
    c = {**_r("-Classe de constructions", [100, 200, 400, 220], 0.9)}
    d_ = {**_r("-Coefficient de priorité", [100, 224, 400, 244], 0.9)}
    out = extract.group_paragraphs([a, b, c, d_], [], [])
    texts = sorted(o["source_text"] for o in out)
    assert "Un plan de détection doit être élaboré et faire l'objet de validation." in texts
    assert "-Classe de constructions" in texts and "-Coefficient de priorité" in texts


def test_centred_two_line_label_joins_across_short_stroke():
    a = {**_r("Salle de", [926, 1261, 1216, 1340], 0.9), "origin": "native"}
    b = {**_r("repos", [968, 1357, 1172, 1436], 0.9), "origin": "native"}
    furniture_top = [(1350, 960, 1180)]           # shorter than 'Salle de'
    out = extract.group_paragraphs([a, b], [], furniture_top)
    assert [o["source_text"] for o in out] == ["Salle de repos"] and out[0]["line_align"] == "centre"


def test_table_row_border_still_separates_lines():
    a = _r("Longueur de", [100, 100, 300, 120], 0.9)
    b = _r("Recouvrement", [100, 126, 320, 146], 0.9)
    border = [(123, 60, 400)]                      # spans past both lines
    walls = [(60, 80, 160), (400, 80, 160)]        # cell walls left and right
    out = extract.group_paragraphs([a, b], walls, border)
    assert len(out) == 2


def test_wall_line_through_two_line_label_does_not_split_it():
    a = _r("Dalle de couverture", [1983, 2728, 2206, 2746], 0.95)
    b = _r("coulée en place", [1984, 2750, 2160, 2770], 0.9)
    wall = [(2748, 1820, 6310)]                    # building outline between the lines
    out = extract.group_paragraphs([a, b], [(1820, 2580, 2750), (6310, 2580, 2750)], wall)
    assert [o["source_text"] for o in out] == ["Dalle de couverture coulée en place"]


def test_underline_stored_with_label_is_redrawn_letters_are_not():
    from core.pdf_vector.writeback import path_sigs, replay_non_glyph
    d = fitz.open()
    p = d.new_page(width=300, height=200)
    sh = p.new_shape()
    for i in range(4):                                  # letter strokes of one label ...
        sh.draw_line((50 + i * 12, 50), (50 + i * 12, 62))
    sh.draw_line((50, 63), (90, 63))                    # ... its underline, in the same path ...
    sh.draw_line((96, 56), (250, 150))                  # ... and a leader leaving the line
    sh.finish(color=(0, 0, 0), width=0.5)
    sh.commit()
    src_draw: dict = {}
    sigs = path_sigs(p, src_draw)
    row = {"id": "R1", "origin": "ocr", "display_box": [48, 50, 92, 62], "display_rotation": 0}
    apply_removal(p, [row], DPI)
    replay_non_glyph(p, sigs, src_draw, [row], DPI)
    segs = {(round(it[1].x), round(it[1].y), round(it[2].x), round(it[2].y))
            for dr in p.get_drawings() for it in dr["items"] if it[0] == "l"}
    assert (50, 63, 90, 63) in segs                     # underline back
    assert any(s[2] == 250 or s[0] == 250 for s in segs)  # leader kept
    assert not any(s[0] == s[2] and 50 <= s[0] <= 86 and s[1] == 50 for s in segs)  # letters gone
