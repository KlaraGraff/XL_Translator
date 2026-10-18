"""PPT 译文放不下自动缩小（core/pptx_fit.py）。

样例 tests/fixtures/pptx_fit/samples_zh.pptx 里每个要翻的框都是「中文刚好放满」，译成法文后
按包 2 的写法换字（仅译文：整段写进第一个片段；双语：原段后接一段译文），再调
``fit_after_write``。默认不给字体目录，走估算字宽，结果与机器上装了什么字体无关。
"""
from __future__ import annotations

import copy
import json
import zipfile
from pathlib import Path

import pytest
from lxml import etree
from PIL import ImageFont

from core import pptx_fit
from core.font_metrics import FontLibrary
from core.pptx_fit import (
    EMU_PER_PT,
    NORM_AUTOFIT_STEPS,
    REASON_KEEP_COVERS,
    REASON_KEEP_OFF_PAGE,
    REASON_KEEP_OVERFLOW,
    REASON_OVERFLOW,
    FitIssue,
    _match_placeholder,
    _PartContext,
    _Placeholder,
    fit_after_write,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pptx_fit"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
NO_FONTS = FontLibrary([])


@pytest.fixture(scope="module")
def parts() -> dict[str, bytes]:
    with zipfile.ZipFile(FIXTURES / "samples_zh.pptx") as package:
        return {name: package.read(name) for name in package.namelist()}


@pytest.fixture(scope="module")
def french() -> dict[str, list[str]]:
    return json.loads((FIXTURES / "translations_fr.json").read_text("utf-8"))


def _write(paragraph, text: str) -> None:
    runs = paragraph.findall(A + "r")
    rpr = copy.deepcopy(runs[0].find(A + "rPr")) if runs and runs[0].find(A + "rPr") is not None else None
    for child in list(paragraph):
        if child.tag in (A + "r", A + "br", A + "fld"):
            paragraph.remove(child)
    run = etree.Element(A + "r")
    if rpr is None:
        rpr = etree.Element(A + "rPr")
    rpr.set("lang", "fr-FR")
    run.append(rpr)
    etree.SubElement(run, A + "t").text = text
    end = paragraph.find(A + "endParaRPr")
    if end is not None:
        end.addprevious(run)
    else:
        paragraph.append(run)


def _put(paragraph, text: str, bilingual: bool) -> None:
    if not bilingual:
        _write(paragraph, text)
        return
    twin = copy.deepcopy(paragraph)
    _write(twin, text)
    ppr = twin.find(A + "pPr")
    if ppr is None:
        ppr = etree.Element(A + "pPr")
        twin.insert(0, ppr)
    ppr.set("indent", "0")
    ppr.append(etree.Element(A + "buNone"))
    paragraph.addnext(twin)


def _has_text(paragraph) -> bool:
    return bool("".join(t.text or "" for t in paragraph.iter(A + "t")).strip())


def _translate(root, translations, *, bilingual=False):
    touched = {}
    for element in root.iter(P + "sp", P + "graphicFrame"):
        nv = element.find("*/" + P + "cNvPr")
        if nv is None or nv.get("descr") != "fit":
            continue
        texts = list(translations[nv.get("name")])
        if element.tag == P + "sp":
            body = element.find(P + "txBody")
            for paragraph in [p for p in body.findall(A + "p") if _has_text(p)]:
                _put(paragraph, texts.pop(0), bilingual)
            touched[nv.get("name")] = body
        else:
            for tc in element.iter(A + "tc"):
                for paragraph in tc.find(A + "txBody").findall(A + "p"):
                    if _has_text(paragraph):
                        _put(paragraph, texts.pop(0), bilingual)
            touched[nv.get("name")] = element
    return touched


def _run(parts, french, slide, *, bilingual=False, fonts=NO_FONTS, edit=None, layout_mode="fit", **fit_options):
    """``edit`` 改的是原文那份（包里的部件和要写回的都改），模拟另一种原稿。"""
    name = f"ppt/slides/slide{slide}.xml"
    if edit is not None:
        source = etree.fromstring(parts[name])
        edit(source)
        parts = dict(parts, **{name: etree.tostring(source)})
    root = etree.fromstring(parts[name])
    touched = _translate(root, french, bilingual=bilingual)
    issues = fit_after_write(parts.get, name, root, list(touched.values()), fonts=fonts,
                             layout_mode=layout_mode, **fit_options)
    return root, touched, issues


def _shape(root, name):
    for nv in root.iter(P + "cNvPr"):
        if nv.get("name") == name:
            return nv.getparent().getparent()
    raise KeyError(name)


def _sizes(element) -> set[int]:
    return {int(rpr.get("sz")) for rpr in element.iter(A + "rPr", A + "endParaRPr") if rpr.get("sz")}


def _ext(shape) -> tuple[int, int, int, int]:
    xfrm = shape.find(f"{P}spPr/{A}xfrm")
    off, ext = xfrm.find(A + "off"), xfrm.find(A + "ext")
    return int(off.get("x")), int(off.get("y")), int(ext.get("cx")), int(ext.get("cy"))


def _add_rect(root, x, y, cx, cy, shape_id="99"):
    tree = root.find(f"{P}cSld/{P}spTree")
    sp = etree.SubElement(tree, P + "sp")
    nv = etree.SubElement(sp, P + "nvSpPr")
    etree.SubElement(nv, P + "cNvPr", id=shape_id, name=f"obstacle {shape_id}")
    etree.SubElement(nv, P + "cNvSpPr")
    etree.SubElement(nv, P + "nvPr")
    sp_pr = etree.SubElement(sp, P + "spPr")
    xfrm = etree.SubElement(sp_pr, A + "xfrm")
    etree.SubElement(xfrm, A + "off", x=str(x), y=str(y))
    etree.SubElement(xfrm, A + "ext", cx=str(cx), cy=str(cy))
    return sp


# ---------------------------------------------------------------- 不自动调整


def test_no_autofit_box_grows_into_blank_space_first(parts, french):
    before = etree.fromstring(parts["ppt/slides/slide1.xml"])
    root, touched, issues = _run(parts, french, 1)
    x0, y0, cx0, cy0 = _ext(_shape(before, "noauto-free"))
    x1, y1, cx1, cy1 = _ext(_shape(root, "noauto-free"))
    assert (x1, y1, cx1) == (x0, y0, cx0) and cy1 > cy0
    assert _sizes(touched["noauto-free"]) == {1800}  # 下面有空白，字号不动
    assert all(issue.target is not touched["noauto-free"] for issue in issues)


def test_fixed_layout_shrinks_runs_and_keeps_every_box(parts, french):
    before = etree.fromstring(parts["ppt/slides/slide1.xml"])
    root, touched, issues = _run(parts, french, 1, layout_mode="fixed")
    sizes = _sizes(touched["noauto-free"])
    assert len(sizes) == 1 and 1080 <= sizes.pop() < 1800
    assert _ext(_shape(root, "noauto-free")) == _ext(_shape(before, "noauto-free"))
    assert all(issue.target is not touched["noauto-free"] for issue in issues)


def test_no_autofit_box_blocked_below_goes_to_review(parts, french):
    root, touched, issues = _run(parts, french, 1, bilingual=True)
    blocked = touched["noauto-blocked"]
    assert _sizes(blocked) == {1050}  # 18 磅缩到 60%，取到半磅
    assert [issue for issue in issues if issue.target is blocked] == [
        FitIssue("ppt/slides/slide1.xml", blocked, REASON_OVERFLOW)
    ]
    assert REASON_OVERFLOW == "缩到 60% 仍放不下"


def test_no_autofit_box_grows_to_shape_below_then_shrinks(parts, french):
    def block(root):
        x, y, cx, cy = _ext(_shape(root, "noauto-free"))
        _add_rect(root, x, y + cy + int(0.3 * 914400), cx, 914400)

    root, touched, issues = _run(parts, french, 1, edit=block)
    x, y, cx, cy = _ext(_shape(root, "noauto-free"))
    obstacle = _shape(root, "obstacle 99")
    assert y + cy <= _ext(obstacle)[1]
    assert cy > _ext(_shape(etree.fromstring(parts["ppt/slides/slide1.xml"]), "noauto-free"))[3]
    sizes = _sizes(touched["noauto-free"])
    assert len(sizes) == 1 and 1080 <= sizes.pop() < 1800
    assert all(issue.target is not touched["noauto-free"] for issue in issues)


# ---------------------------------------------------------------- 溢出时缩排文字


def test_norm_autofit_steps_follow_powerpoint():
    assert NORM_AUTOFIT_STEPS == ((1.0, 0.0), (0.925, 0.0), (0.85, 0.1), (0.775, 0.1), (0.7, 0.2), (0.625, 0.2))


def test_placeholders_get_recomputed_font_scale(parts, french):
    root, touched, issues = _run(parts, french, 2)
    assert issues == []
    for name in ("title", "body"):
        body_pr = touched[name].find(A + "bodyPr")
        norm = body_pr.find(A + "normAutofit")
        assert norm is not None, name
        step = (int(norm.get("fontScale", "100000")) / 100000, int(norm.get("lnSpcReduction", "0")) / 100000)
        assert step in NORM_AUTOFIT_STEPS and step[0] < 1
        assert _sizes(touched[name]) == set()  # 字号本身不动，PowerPoint 按比例画


def test_stale_font_scale_is_replaced(parts, french):
    def stale(root):
        body_pr = _shape(root, "title").find(f"{P}txBody/{A}bodyPr")
        etree.SubElement(body_pr, A + "normAutofit", fontScale="25000", lnSpcReduction="20000")

    short = dict(french, title=["Avancement"], body=["Fin"] * 7)
    root, touched, _issues = _run(parts, short, 2, edit=stale)
    norms = touched["title"].find(A + "bodyPr").findall(A + "normAutofit")
    assert len(norms) == 1 and dict(norms[0].attrib) == {}  # 放得下：回到 100%，不沿用原文的比例


def test_bilingual_body_too_long_goes_to_review(parts, french):
    _root, touched, issues = _run(parts, french, 2, bilingual=True)
    norm = touched["body"].find(f"{A}bodyPr/{A}normAutofit")
    assert norm.get("fontScale") == "62500" and norm.get("lnSpcReduction") == "20000"
    assert [issue.target for issue in issues] == [touched["body"]]


# ---------------------------------------------------------------- 根据文字调整形状大小


def test_auto_resize_box_grows_down_without_shrinking(parts, french):
    before = etree.fromstring(parts["ppt/slides/slide3.xml"])
    root, touched, issues = _run(parts, french, 3)
    x0, y0, cx0, cy0 = _ext(_shape(before, "spauto-free"))
    x1, y1, cx1, cy1 = _ext(_shape(root, "spauto-free"))
    assert (x1, y1, cx1) == (x0, y0, cx0) and cy1 > cy0
    assert _sizes(touched["spauto-free"]) == {1800}
    assert issues == []


def test_unwrapped_label_widens_to_the_right(parts, french):
    before = etree.fromstring(parts["ppt/slides/slide3.xml"])
    root, touched, _issues = _run(parts, french, 3)
    x0, y0, cx0, cy0 = _ext(_shape(before, "spauto-label"))
    x1, y1, cx1, cy1 = _ext(_shape(root, "spauto-label"))
    assert (x1, y1, cy1) == (x0, y0, cy0) and cx1 > cx0
    assert _sizes(touched["spauto-label"]) == {1800}


def test_fixed_layout_keeps_auto_resize_box_and_shrinks(parts, french):
    before = etree.fromstring(parts["ppt/slides/slide3.xml"])
    root, touched, _issues = _run(parts, french, 3, layout_mode="fixed")
    for name in ("spauto-free", "spauto-label"):
        assert _ext(_shape(root, name)) == _ext(_shape(before, name)), name
        assert max(_sizes(touched[name])) < 1800, name


def test_auto_resize_box_stops_at_shape_below_then_shrinks(parts, french):
    root, touched, _issues = _run(parts, french, 3)
    shape = _shape(root, "spauto-near")
    obstacle = next(sp for sp in root.iter(P + "sp") if sp.find(f"{P}nvSpPr/{P}cNvSpPr") is not None
                    and "下方已有内容" in "".join(t.text or "" for t in sp.iter(A + "t")))
    x, y, cx, cy = _ext(shape)
    assert y + cy <= _ext(obstacle)[1]
    assert 1080 <= max(_sizes(touched["spauto-near"])) < 1800


# ---------------------------------------------------------------- 表格、组合、竖排、旋转


def test_table_grows_into_blank_space_first(parts, french):
    _root, touched, issues = _run(parts, french, 4)
    assert _sizes(touched["table"]) == {1800}
    assert issues == []


def test_table_shrinks_as_a_whole_when_blocked(parts, french):
    def block(root):
        frame = root.find(f".//{P}graphicFrame")
        off, ext = frame.find(f"{P}xfrm/{A}off"), frame.find(f"{P}xfrm/{A}ext")
        bottom = int(off.get("y")) + int(ext.get("cy"))
        _add_rect(root, int(off.get("x")), bottom + int(0.6 * 914400), int(ext.get("cx")), 914400)

    _root, touched, issues = _run(parts, french, 4, edit=block)
    sizes = _sizes(touched["table"])
    assert len(sizes) == 1 and 1080 <= sizes.pop() < 1800  # 整张表一个字号
    assert issues == []


def test_fixed_layout_table_shrinks_into_its_rows(parts, french):
    """固定版面不往下长；「外墙保温」译成两行，缩到 60% 也超出原行高 2 磅，进复核。"""
    _root, touched, issues = _run(parts, french, 4, layout_mode="fixed")
    assert _sizes(touched["table"]) == {1050}
    assert [issue.target for issue in issues] == [touched["table"]]
    roomy = french | {"table": [text.replace("Isolation thermique des façades", "Isolation")
                                for text in french["table"]]}
    _root, touched, issues = _run(parts, roomy, 4, layout_mode="fixed")
    sizes = _sizes(touched["table"])
    assert len(sizes) == 1 and min(sizes) < 1800  # 整表缩进原来的行高里
    assert issues == []


def test_table_blocked_right_below_goes_to_review(parts, french):
    def block(root):
        frame = root.find(f".//{P}graphicFrame")
        off, ext = frame.find(f"{P}xfrm/{A}off"), frame.find(f"{P}xfrm/{A}ext")
        _add_rect(root, int(off.get("x")), int(off.get("y")) + int(ext.get("cy")), int(ext.get("cx")), 914400)

    _root, touched, issues = _run(parts, french, 4, edit=block)
    assert _sizes(touched["table"]) == {1050}
    assert [issue.target for issue in issues] == [touched["table"]]


def test_group_shapes_scale_with_group_and_never_grow(parts, french):
    before = etree.fromstring(parts["ppt/slides/slide5.xml"])
    root, touched, _issues = _run(parts, french, 5)
    for name in ("group-a", "group-b"):
        assert _ext(_shape(root, name)) == _ext(_shape(before, name))
    assert max(_sizes(touched["group-a"])) < 1800


# ---------------------------------------------------------------- 保持字号


def _reasons(root, issues) -> dict[str, str]:
    names = {}
    for issue in issues:
        holder = issue.target if issue.target.tag == P + "graphicFrame" else issue.target.getparent()
        names[holder.find("*/" + P + "cNvPr").get("name")] = issue.reason
    return names


def test_keep_size_grows_every_box_and_lists_what_it_covers(parts, french):
    before = etree.fromstring(parts["ppt/slides/slide1.xml"])
    root, touched, issues = _run(parts, french, 1, bilingual=True, layout_mode="keep_size")
    for name in ("noauto-free", "noauto-blocked"):
        assert _sizes(touched[name]) == {1800}, name
        x0, y0, cx0, cy0 = _ext(_shape(before, name))
        x1, y1, cx1, cy1 = _ext(_shape(root, name))
        assert (x1, y1, cx1) == (x0, y0, cx0) and cy1 > cy0, name
    assert _reasons(root, issues) == {"noauto-blocked": REASON_KEEP_COVERS}


def test_keep_size_placeholder_drops_font_scale_and_may_leave_the_page(parts, french):
    root, touched, issues = _run(parts, french, 2, bilingual=True, layout_mode="keep_size")
    for name in ("title", "body"):
        norm = touched[name].find(f"{A}bodyPr/{A}normAutofit")
        assert norm is not None and dict(norm.attrib) == {}, name
    reasons = _reasons(root, issues)
    assert reasons.get("body") == REASON_KEEP_OFF_PAGE
    assert set(reasons) <= {"title", "body"}


def test_keep_size_table_and_groups(parts, french):
    def block(root):
        frame = root.find(f".//{P}graphicFrame")
        off, ext = frame.find(f"{P}xfrm/{A}off"), frame.find(f"{P}xfrm/{A}ext")
        _add_rect(root, int(off.get("x")), int(off.get("y")) + int(ext.get("cy")), int(ext.get("cx")), 914400)

    root, touched, issues = _run(parts, french, 4, layout_mode="keep_size", edit=block)
    assert _sizes(touched["table"]) == {1800}
    assert _reasons(root, issues) == {"table": REASON_KEEP_COVERS}
    root, touched, issues = _run(parts, french, 5, bilingual=True, layout_mode="keep_size")
    assert _sizes(touched["group-a"]) == {1800}
    assert set(_reasons(root, issues).values()) <= {REASON_KEEP_OVERFLOW}
    assert _reasons(root, issues)  # 组合里的框不能加高，放不下就列出来


def test_keep_size_label_widens_on_the_page(parts, french):
    before = etree.fromstring(parts["ppt/slides/slide3.xml"])
    root, touched, issues = _run(parts, french, 3, layout_mode="keep_size")
    x0, y0, cx0, cy0 = _ext(_shape(before, "spauto-label"))
    x1, y1, cx1, cy1 = _ext(_shape(root, "spauto-label"))
    assert (x1, y1, cy1) == (x0, y0, cy0) and cx1 > cx0
    assert _sizes(touched["spauto-label"]) == {1800}
    assert "spauto-label" not in _reasons(root, issues)


# ---------------------------------------------------------------- 智能换行


def test_unwrapped_label_wraps_when_it_cannot_widen(parts, french):
    """右边紧挨着别的形状、加宽不了：缩到 60% 一行也放不下，改成折行往下长，字号不变。"""
    def hem_in(root):
        x, y, cx, cy = _ext(_shape(root, "spauto-label"))
        _add_rect(root, x + cx, y, 914400, cy)

    before = etree.fromstring(parts["ppt/slides/slide3.xml"])
    root, touched, issues = _run(parts, french, 3, edit=hem_in)
    body_pr = touched["spauto-label"].find(A + "bodyPr")
    assert body_pr.get("wrap") == "square"
    x0, y0, cx0, cy0 = _ext(_shape(before, "spauto-label"))
    x1, y1, cx1, cy1 = _ext(_shape(root, "spauto-label"))
    assert (x1, y1, cx1) == (x0, y0, cx0) and cy1 > cy0
    assert _sizes(touched["spauto-label"]) == {1800}
    assert "spauto-label" not in _reasons(root, issues)


def test_fixed_layout_wraps_a_tall_unwrapped_box_instead_of_shrinking_further(parts, french):
    def taller(root):
        _shape(root, "spauto-label").find(f"{P}spPr/{A}xfrm/{A}ext").set("cy", str(int(0.9 * 914400)))

    before = etree.fromstring(parts["ppt/slides/slide3.xml"])
    root, touched, issues = _run(parts, french, 3, edit=taller, layout_mode="fixed")
    assert touched["spauto-label"].find(A + "bodyPr").get("wrap") == "square"
    x0, y0, cx0, _cy0 = _ext(_shape(before, "spauto-label"))
    assert _ext(_shape(root, "spauto-label")) == (x0, y0, cx0, int(0.9 * 914400))  # 框不动
    assert min(_sizes(touched["spauto-label"])) > 1050
    assert "spauto-label" not in _reasons(root, issues)


def test_fixed_layout_wraps_a_label_that_spills_less_wrapped(parts, french):
    """固定版面：缩到 60% 一行往右冒出一截，折成两行只往下多一点，就折行；没完全放下照样进复核。"""
    before = etree.fromstring(parts["ppt/slides/slide3.xml"])
    root, touched, issues = _run(parts, french, 3, layout_mode="fixed")
    assert touched["spauto-label"].find(A + "bodyPr").get("wrap") == "square"
    assert _ext(_shape(root, "spauto-label")) == _ext(_shape(before, "spauto-label"))
    assert _reasons(root, issues)["spauto-label"] == REASON_OVERFLOW


def _one_line_width(parts, french, name: str, scale: float) -> float:
    """译文在 ``name`` 里排成一行、按 ``scale`` 缩字时的宽度（磅）。"""
    part = "ppt/slides/slide3.xml"
    root = etree.fromstring(parts[part])
    _translate(root, french)
    sp = _shape(root, name)
    ctx = _PartContext(parts.get, part, root, NO_FONTS)
    paras, atoms = pptx_fit._measure_shape(ctx, sp, sp.find(P + "txBody"))
    return pptx_fit._layout(paras, atoms, None, pptx_fit.explicit_sizer(scale), line_scale=scale).widest


def test_label_stays_on_one_line_when_wrapping_spills_more(parts, french):
    """框只比 60% 的一行窄一点：一行往右冒 1 磅，折行会往下多出一行的一部分，保留一行。"""
    insets = 2 * 91440
    width = int((_one_line_width(parts, french, "spauto-label", pptx_fit.MIN_SCALE) - 1) * EMU_PER_PT) + insets

    def narrower(root):
        _shape(root, "spauto-label").find(f"{P}spPr/{A}xfrm/{A}ext").set("cx", str(width))

    root, touched, issues = _run(parts, french, 3, edit=narrower, layout_mode="fixed")
    assert touched["spauto-label"].find(A + "bodyPr").get("wrap") == "none"
    assert _reasons(root, issues)["spauto-label"] == REASON_OVERFLOW


def test_grown_boxes_keep_the_chosen_gap(parts, french):
    """自适应排版：往下长的框停在下方形状上沿往上 gap 处（默认 2 磅）；gap 为 0 时可以贴上去。"""
    obstacle = _ext(_shape(etree.fromstring(parts["ppt/slides/slide3.xml"]), "Rectangle 5"))
    default_root, _touched, _issues = _run(parts, french, 3, bilingual=True)
    x, y, cx, cy = _ext(_shape(default_root, "spauto-near"))
    assert obstacle[1] - (y + cy) >= 2 * pptx_fit.EMU_PER_PT - 1
    touching_root, _touched, _issues = _run(parts, french, 3, bilingual=True, gap_pt=0)
    x, y, cx, cy = _ext(_shape(touching_root, "spauto-near"))
    assert obstacle[1] - (y + cy) < 2 * pptx_fit.EMU_PER_PT
    assert y + cy <= obstacle[1]
    # 原稿里本来就比间距还近的，不再往那边长：间距设成很大时这个框一点也不长。
    original = _ext(_shape(etree.fromstring(parts["ppt/slides/slide3.xml"]), "spauto-near"))
    far_root, _touched, _issues = _run(parts, french, 3, bilingual=True, gap_pt=300)
    assert _ext(_shape(far_root, "spauto-near"))[3] == original[3]


def test_vertical_box_untouched_and_rotated_box_listed(parts, french):
    root, touched, issues = _run(parts, french, 6)
    assert _sizes(touched["vertical"]) == {1800}
    assert _sizes(touched["rotated"]) == {1050}
    assert [issue.target for issue in issues] == [touched["rotated"]]


# ---------------------------------------------------------------- 继承与边界


def test_theme_fonts_and_placeholder_geometry_inherited(parts):
    name = "ppt/slides/slide2.xml"
    root = etree.fromstring(parts[name])
    ctx = _PartContext(parts.get, name, root, NO_FONTS)
    assert ctx.theme.resolve("+mn-lt", "zh-CN") == "Calibri"
    assert ctx.theme.resolve("+mj-ea", "zh-CN") == "宋体"
    assert ctx.theme.resolve("+mn-ea", "ja-JP") == "ＭＳ Ｐゴシック"
    title = _shape(root, "title")
    assert title.find(f"{P}spPr/{A}xfrm") is None
    rect = pptx_fit._shape_rect(ctx, title)
    assert rect is not None and rect.cx > 0
    reader = pptx_fit._TextBodyReader(ctx, ctx.style_layers(title))
    paras = reader.paragraphs(title.find(P + "txBody"))
    assert paras[0].pieces[0][1].size == 44.0  # 母版标题样式
    assert paras[0].pieces[0][1].latin == "Calibri"


def test_placeholder_matching_order():
    table = [
        _Placeholder("dt", "2", "date"),
        _Placeholder("body", "1", "body"),
        _Placeholder("title", "", "title"),
    ]
    assert _match_placeholder(table, "body", "1").sp == "body"
    assert _match_placeholder(table, "obj", "1").sp == "body"  # 按 idx
    assert _match_placeholder(table, "body", "2").sp == "body"  # idx 对上的是日期，跳过，按 type
    assert _match_placeholder(table, "ctrTitle", "").sp == "title"  # 同一类
    assert _match_placeholder(table, "pic", "9") is None


def test_original_overflow_is_not_held_against_translation(parts, french):
    """原文本来就超出框（作者接受了），译文排到同样高度不算放不下。"""
    name = "ppt/slides/slide1.xml"
    source = etree.fromstring(parts[name])
    shape = _shape(source, "noauto-free")
    ext = shape.find(f"{P}spPr/{A}xfrm/{A}ext")
    ext.set("cy", str(int(ext.get("cy")) // 3))
    shape.find(f".//{A}t").text = "本项目" * 60
    edited = dict(parts, **{name: etree.tostring(source)})
    root = copy.deepcopy(source)
    body = _shape(root, "noauto-free").find(P + "txBody")
    _write(body.find(A + "p"), "Projet " * 22)
    fit_after_write(edited.get, name, root, [body], fonts=NO_FONTS)
    assert _sizes(body) == {1800}


def test_real_font_widths_are_used(parts, french, tmp_path):
    data = getattr(ImageFont.load_default(size=10), "font_bytes", None)
    if not data:
        pytest.skip("Pillow 没有带 FreeType 字体")
    (tmp_path / "Aileron.ttf").write_bytes(data)
    name = "ppt/slides/slide1.xml"
    root = etree.fromstring(parts[name])
    ctx = _PartContext(parts.get, name, root, FontLibrary([tmp_path]))
    body = _shape(root, "noauto-free").find(P + "txBody")
    for latin in body.iter(A + "latin"):
        latin.set("typeface", "Aileron")
    _write(body.find(A + "p"), "WWWW")
    paras, atoms = pptx_fit._measure_shape(ctx, body.getparent(), body)
    layout = pptx_fit._layout(paras, atoms, None, pptx_fit.autofit_sizer(1.0))
    pil = ImageFont.truetype(str(tmp_path / "Aileron.ttf"), size=1000)
    assert layout.widest == pytest.approx(4 * pil.getlength("W") / 1000 * 18, abs=0.05)


def test_notes_and_unknown_parts_are_left_alone(parts):
    root = etree.fromstring(parts["ppt/slides/slide1.xml"])
    body = root.find(f".//{P}txBody")
    snapshot = etree.tostring(root)
    assert fit_after_write(parts.get, "ppt/notesSlides/notesSlide1.xml", root, [body]) == []
    assert fit_after_write(parts.get, "ppt/slides/slide1.xml", root, []) == []
    assert etree.tostring(root) == snapshot


def test_missing_parts_fall_back_to_defaults(parts, french):
    """读不到版式、母版、主题时按默认值算，不报错。"""
    name = "ppt/slides/slide1.xml"
    root = etree.fromstring(parts[name])
    touched = _translate(root, french)
    issues = fit_after_write(lambda part: None, name, root, list(touched.values()), fonts=NO_FONTS)
    assert all(issue.reason == REASON_OVERFLOW for issue in issues)
    shape = _shape(root, "noauto-free")
    source = _shape(etree.fromstring(parts[name]), "noauto-free")
    assert max(_sizes(touched["noauto-free"])) < 1800 or _ext(shape)[3] > _ext(source)[3]


def test_output_stays_well_formed(parts, french):
    for slide in range(1, 7):
        root, _touched, _issues = _run(parts, french, slide, bilingual=True)
        data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        reparsed = etree.fromstring(data)
        for body_pr in reparsed.iter(A + "bodyPr"):
            autofits = [c for c in body_pr if etree.QName(c).localname in ("noAutofit", "normAutofit", "spAutoFit")]
            assert len(autofits) <= 1
        for sp_pr in reparsed.iter(P + "spPr"):
            xfrm = sp_pr.find(A + "xfrm")
            assert xfrm is None or sp_pr.index(xfrm) == 0
        for run in reparsed.iter(A + "r"):
            rpr = run.find(A + "rPr")
            assert rpr is None or run.index(rpr) == 0
        for paragraph in reparsed.iter(A + "p"):
            end = paragraph.find(A + "endParaRPr")
            assert end is None or paragraph.index(end) == len(paragraph) - 1


def test_emu_per_point():
    assert EMU_PER_PT * 72 == 914400
