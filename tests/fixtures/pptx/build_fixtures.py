"""Rebuild the .pptx test fixtures and the samples of the bundled PPT type.

Needs python-pptx, which the app does not ship and the tests do not import::

    ./.venv/bin/python3 -m pip install python-pptx
    ./.venv/bin/python3 tests/fixtures/pptx/build_fixtures.py

python-pptx lays out the slides; what it cannot make (fields, paragraph
animations, auto-numbering, vertical text, ``mc:AlternateContent``, comments,
text on the master and layouts) is added to the XML afterwards.  Every text is
written for these files; nothing comes from other projects.
"""

from __future__ import annotations

import io
import shutil
import zipfile
from copy import deepcopy
from pathlib import Path

from lxml import etree
from PIL import Image, ImageDraw
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Emu, Inches, Pt

HERE = Path(__file__).resolve().parent
SAMPLES = HERE.parents[2] / "adapters" / "pptx" / "samples"

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}
A = "{%s}" % NS["a"]
P = "{%s}" % NS["p"]


def _textbox(slide, left, top, width, height, text, size=18):
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    frame = box.text_frame
    frame.word_wrap = True
    frame.text = text
    for paragraph in frame.paragraphs:
        for run in paragraph.runs:
            run.font.size = Pt(size)
    return box


_FIXED_TEXT = """<p:sp xmlns:p="{p}" xmlns:a="{a}"><p:nvSpPr><p:cNvPr id="{id}" name="文本框 {id}"/><p:cNvSpPr txBox="1"/>
<p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>
<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr><p:txBody><a:bodyPr wrap="square" rtlCol="0">
<a:spAutoFit/></a:bodyPr><a:lstStyle/><a:p><a:r><a:rPr lang="zh-CN" altLang="en-US" sz="1000"/><a:t>{text}</a:t></a:r>
<a:endParaRPr lang="zh-CN" altLang="en-US" sz="1000"/></a:p></p:txBody></p:sp>"""


def _fixed_text(owner, shape_id: int, text: str, x, y, cx, cy) -> None:
    """母版或版式上的一个普通文本框（python-pptx 只能往幻灯片上加形状）。"""
    tree = owner._element.find(f"{P}cSld/{P}spTree")
    tree.append(
        etree.fromstring(
            _FIXED_TEXT.format(p=NS["p"], a=NS["a"], id=shape_id, text=text, x=int(x), y=int(y), cx=int(cx), cy=int(cy))
        )
    )


def _picture() -> io.BytesIO:
    image = Image.new("RGB", (320, 200), (230, 238, 246))
    ImageDraw.Draw(image).rectangle((20, 20, 300, 180), outline=(40, 90, 160), width=6)
    data = io.BytesIO()
    image.save(data, "PNG")
    data.seek(0)
    return data


def _field_paragraph(box, before: str, after: str) -> None:
    """``before`` + 页码字段 + ``after``（python-pptx 不会写字段）。"""
    paragraph = box.text_frame.paragraphs[0]._p
    for child in list(paragraph):
        if child.tag in (A + "r", A + "fld", A + "br"):
            paragraph.remove(child)
    end = paragraph.find(A + "endParaRPr")

    def add(element):
        if end is not None:
            end.addprevious(element)
        else:
            paragraph.append(element)

    run = etree.Element(A + "r")
    etree.SubElement(run, A + "rPr", lang="zh-CN", sz="1400")
    etree.SubElement(run, A + "t").text = before
    add(run)
    field = etree.Element(A + "fld", id="{B6F15528-21DE-4FAA-801E-634DDDAF4B2B}", type="slidenum")
    etree.SubElement(field, A + "rPr", lang="zh-CN", sz="1400")
    etree.SubElement(field, A + "t").text = "3"
    add(field)
    run = etree.Element(A + "r")
    etree.SubElement(run, A + "rPr", lang="zh-CN", sz="1400")
    etree.SubElement(run, A + "t").text = after
    add(run)


def _numbered(box) -> None:
    for paragraph in box.text_frame.paragraphs:
        ppr = paragraph._p.get_or_add_pPr()
        ppr.set("marL", "342900")
        ppr.set("indent", "-342900")
        etree.SubElement(ppr, A + "buFont", typeface="+mj-lt")
        etree.SubElement(ppr, A + "buAutoNum", type="arabicPeriod")


_TIMING = """<p:timing xmlns:p="{p}"><p:tnLst><p:par><p:cTn id="1" dur="indefinite" restart="never" nodeType="tmRoot">
<p:childTnLst><p:seq concurrent="1" nextAc="seek"><p:cTn id="2" dur="indefinite" nodeType="mainSeq"><p:childTnLst>
{pars}
</p:childTnLst></p:cTn><p:prevCondLst><p:cond evt="onPrev" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:prevCondLst>
<p:nextCondLst><p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:nextCondLst></p:seq></p:childTnLst>
</p:cTn></p:par></p:tnLst><p:bldLst><p:bldP spid="{spid}" grpId="0" build="p"/></p:bldLst></p:timing>"""

_STEP = """<p:par><p:cTn id="{a}" fill="hold"><p:stCondLst><p:cond delay="indefinite"/></p:stCondLst><p:childTnLst>
<p:par><p:cTn id="{b}" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>
<p:par><p:cTn id="{c}" presetID="1" presetClass="entr" presetSubtype="0" fill="hold" grpId="0" nodeType="clickEffect">
<p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst><p:set><p:cBhvr><p:cTn id="{d}" dur="1" fill="hold">
<p:stCondLst><p:cond delay="0"/></p:stCondLst></p:cTn><p:tgtEl><p:spTgt spid="{spid}"><p:txEl><p:pRg st="{i}" end="{i}"/></p:txEl>
</p:spTgt></p:tgtEl><p:attrNameLst><p:attrName>style.visibility</p:attrName></p:attrNameLst></p:cBhvr>
<p:to><p:strVal val="visible"/></p:to></p:set></p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn></p:par>
</p:childTnLst></p:cTn></p:par>"""


def _animate_paragraphs(slide, shape, count: int) -> None:
    spid = shape.shape_id
    steps = "".join(
        _STEP.format(a=3 + 4 * i, b=4 + 4 * i, c=5 + 4 * i, d=6 + 4 * i, spid=spid, i=i) for i in range(count)
    )
    timing = etree.fromstring(_TIMING.format(p=NS["p"], pars=steps, spid=spid))
    slide._element.append(timing)


def _alternate(shape) -> None:
    """把形状包进 ``mc:AlternateContent``（Choice 要求 a14，Fallback 是同一个形状）。"""
    element = shape._element
    parent = element.getparent()
    index = parent.index(element)
    alternate = etree.Element("{%s}AlternateContent" % NS["mc"], nsmap={"mc": NS["mc"]})
    choice = etree.SubElement(
        alternate,
        "{%s}Choice" % NS["mc"],
        Requires="a14",
        nsmap={"a14": "http://schemas.microsoft.com/office/drawing/2010/main"},
    )
    fallback = etree.SubElement(alternate, "{%s}Fallback" % NS["mc"])
    parent.remove(element)
    choice.append(element)
    fallback.append(deepcopy(element))
    parent.insert(index, alternate)


def build_mixed(path: Path) -> None:
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)

    # 母版和版式上的固定文字（不是占位符）。
    _fixed_text(prs.slide_master, 100, "内部资料 请勿外传", Inches(10.6), Inches(7.0), Inches(2.5), Inches(0.4))
    _fixed_text(prs.slide_layouts[1], 100, "卡萨布兰卡新城项目", Inches(0.3), Inches(7.0), Inches(3), Inches(0.4))

    # 1 标题页：标题里有换行。
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = "2026 年度项目进展汇报\v第三季度"
    slide.placeholders[1].text = "工程管理部"

    # 2 正文：项目符号、句中加粗、超链接、两段备注。
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "项目概况"
    body = slide.placeholders[1].text_frame
    body.text = "本项目位于卡萨布兰卡，总建筑面积 12 万平方米。"
    paragraph = body.add_paragraph()
    run = paragraph.add_run()
    run.text = "注意："
    run.font.bold = True
    paragraph.add_run().text = "所有图纸以最新版本为准。"
    paragraph = body.add_paragraph()
    paragraph.text = "主体结构已完成 85%，计划年底封顶。"
    paragraph.level = 1
    paragraph = body.add_paragraph()
    paragraph.add_run().text = "详细资料见"
    run = paragraph.add_run()
    run.text = "项目网站"
    run.hyperlink.address = "https://example.com/project"
    paragraph.add_run().text = "。"
    notes = slide.notes_slide.notes_text_frame
    notes.text = "讲解时强调工期节点。"
    notes.add_paragraph().text = "第二段备注：质量控制要点。"

    # 3 表格 + 页码字段。
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "施工进度"
    rows = [("序号", "工作内容", "负责单位"), ("1", "基础施工", "中建一局"), ("2", "钢结构安装", "安装公司")]
    table = slide.shapes.add_table(3, 3, Inches(1), Inches(1.8), Inches(11), Inches(2.4)).table
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    box = _textbox(slide, 10.5, 6.6, 2.5, 0.5, "x", size=14)
    _field_paragraph(box, "第 ", " 页")

    # 4 组合、形状、图片。
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    group = slide.shapes.add_group_shape()
    first = group.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    first.text_frame.text = "施工准备"
    second = group.shapes.add_textbox(Inches(1), Inches(2.2), Inches(4), Inches(1))
    second.text_frame.text = "现场布置"
    rectangle = slide.shapes.add_shape(1, Inches(6), Inches(1), Inches(4), Inches(1.5))
    rectangle.text_frame.text = "关键路径"
    rectangle.fill.solid()
    rectangle.fill.fore_color.rgb = RGBColor(0x2F, 0x5D, 0x9E)
    slide.shapes.add_picture(_picture(), Inches(6), Inches(3), Inches(3.2), Inches(2))

    # 5 按段落出现的动画。
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "下一步工作"
    body_shape = slide.placeholders[1]
    body = body_shape.text_frame
    body.text = "完成机电招标"
    body.add_paragraph().text = "启动幕墙深化设计"
    body.add_paragraph().text = "组织消防专项验收"
    _animate_paragraphs(slide, body_shape, 3)

    # 6 自动编号。
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = _textbox(slide, 1, 1, 8, 3, "提交施工方案", size=20)
    box.text_frame.add_paragraph().text = "办理开工许可"
    box.text_frame.add_paragraph().text = "进场施工"
    _numbered(box)

    # 7 竖排文字框；8 mc:AlternateContent 里的形状。
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = _textbox(slide, 1, 1, 1, 4, "竖排标题", size=24)
    box.text_frame._txBody.find(A + "bodyPr").set("vert", "eaVert")
    other = _textbox(slide, 3, 1, 6, 1, "兼容内容里的文字", size=20)
    _alternate(other)

    prs.save(str(path))
    _add_comment(path, slide_number=2, text="请核对面积数据。")


def _add_comment(path: Path, *, slide_number: int, text: str) -> None:
    """给第 ``slide_number`` 页加一条旧式批注（作者表 + 批注部件 + 关系 + 内容类型）。"""
    rel_comments = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
    rel_authors = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/commentAuthors"
    authors = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<p:cmAuthorLst xmlns:p="{NS["p"]}">'
        '<p:cmAuthor id="0" name="审核人" initials="SH" lastIdx="1" clrIdx="0"/></p:cmAuthorLst>'
    )
    comments = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<p:cmLst xmlns:p="{NS["p"]}">'
        f'<p:cm authorId="0" dt="2026-10-17T10:00:00.000" idx="1"><p:pos x="10" y="10"/><p:text>{text}</p:text></p:cm>'
        "</p:cmLst>"
    )
    temp = path.with_suffix(".tmp")
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "[Content_Types].xml":
                data = data.replace(
                    b"</Types>",
                    b'<Override PartName="/ppt/commentAuthors.xml" ContentType="application/vnd.openxmlformats-'
                    b'officedocument.presentationml.commentAuthors+xml"/><Override PartName="/ppt/comments/'
                    b'comment1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.'
                    b'comments+xml"/></Types>',
                )
            elif info.filename == "ppt/_rels/presentation.xml.rels":
                data = data.replace(
                    b"</Relationships>",
                    f'<Relationship Id="rId900" Type="{rel_authors}" Target="commentAuthors.xml"/>'
                    "</Relationships>".encode(),
                )
            elif info.filename == f"ppt/slides/_rels/slide{slide_number}.xml.rels":
                data = data.replace(
                    b"</Relationships>",
                    f'<Relationship Id="rId900" Type="{rel_comments}" Target="../comments/comment1.xml"/>'
                    "</Relationships>".encode(),
                )
            target.writestr(info, data)
        target.writestr("ppt/commentAuthors.xml", authors.encode("utf-8"))
        target.writestr("ppt/comments/comment1.xml", comments.encode("utf-8"))
    temp.replace(path)


def build_training(path: Path) -> None:
    """一份简单的讲义：标题、要点、两页备注。"""
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = "安全生产培训"
    slide.placeholders[1].text = "新员工入职第一课"
    slide.notes_slide.notes_text_frame.text = "开场先介绍培训目的。"
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "进入现场前"
    body = slide.placeholders[1].text_frame
    body.text = "佩戴安全帽和反光背心"
    body.add_paragraph().text = "确认当天的作业许可"
    body.add_paragraph().text = "发现隐患立即报告"
    slide.notes_slide.notes_text_frame.text = "提醒学员：没有许可不得进入作业区。"
    prs.save(str(path))


def build_wide_title(path: Path) -> None:
    """一页只有一个刚好放满的标题（缩字和双语对照的排版对比用）。"""
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Emu(914400), Emu(914400), Inches(9), Inches(1))
    box.text_frame.text = "加强质量管理，确保按期交付"
    box.text_frame.paragraphs[0].runs[0].font.size = Pt(32)
    prs.save(str(path))


def main() -> None:
    HERE.mkdir(parents=True, exist_ok=True)
    build_mixed(HERE / "mixed.pptx")
    build_training(HERE / "training.pptx")
    build_wide_title(HERE / "wide_title.pptx")
    SAMPLES.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / "mixed.pptx", SAMPLES / "项目进展汇报.pptx")
    shutil.copyfile(HERE / "training.pptx", SAMPLES / "安全培训讲义.pptx")
    for name in ("mixed.pptx", "training.pptx", "wide_title.pptx"):
        print(name, (HERE / name).stat().st_size)


if __name__ == "__main__":
    main()
