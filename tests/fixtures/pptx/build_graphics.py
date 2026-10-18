"""Rebuild ``graphics.pptx``: charts and a SmartArt for the PPT chart / SmartArt tests.

Needs python-pptx and XlsxWriter (neither ships with the app), plus the SmartArt
source file::

    ./.venv/bin/python3 -m pip install python-pptx XlsxWriter
    curl -LO https://raw.githubusercontent.com/apache/poi/trunk/test-data/slideshow/smartart-simple.pptx
    ./.venv/bin/python3 tests/fixtures/pptx/build_graphics.py smartart-simple.pptx

- Slides 1–3: charts made by python-pptx.  PowerPoint puts an Excel table over
  the chart data in the embedded workbook (python-pptx does not), so the
  workbook of slide 1 is rebuilt with openpyxl with such a table, laid out the
  way PowerPoint lays it out.
- Slide 4: a SmartArt.  Neither python-pptx nor LibreOffice can make one, so the
  diagram parts come from ``smartart-simple.pptx`` in Apache POI's test data
  (Apache License 2.0, saved by PowerPoint for Mac): its four-circle Venn
  diagram, with the English placeholder words replaced by the Chinese texts
  below in both the content part and the drawing part, the drawing scaled up to
  fit the slide.  Everything else in this file is written here.
"""

from __future__ import annotations

import io
import re
import sys
import zipfile
from pathlib import Path

from lxml import etree
from openpyxl import Workbook
from openpyxl.worksheet.table import Table, TableStyleInfo
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.util import Inches, Pt

HERE = Path(__file__).resolve().parent

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "dsp": "http://schemas.microsoft.com/office/drawing/2008/diagram",
}
A = "{%s}" % NS["a"]

QUARTERS = ("第一季度", "第二季度", "第三季度", "第四季度")
#: SmartArt 节点：原文件里 abc / def / ghi 三个圆的文字。def 前两个字加粗，ghi 有两段。
SMARTART_TEXT = {"abc": ["需求调研"], "def": ["方案", "设计"], "ghi": ["施工交付", "竣工验收"]}
SCALE = 5
FONT_SCALE = 2.5


def _chart_slides(prs) -> None:
    # 1 簇状柱形图：标题、坐标轴标题、两个系列、四个类别。
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "季度产值"
    data = CategoryChartData()
    data.categories = QUARTERS
    data.add_series("计划产值", (120, 150, 180, 210))
    data.add_series("实际产值", (110, 160, 175, 0))
    frame = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1.6), Inches(11), Inches(5.4), data
    )
    chart = frame.chart
    chart.has_title = True
    chart.chart_title.text_frame.text = "2026 年各季度产值（万元）"
    chart.has_legend = True
    chart.legend.position = XL_LEGEND_POSITION.BOTTOM
    chart.legend.include_in_layout = False
    chart.category_axis.has_title = True
    chart.category_axis.axis_title.text_frame.text = "季度"
    chart.value_axis.has_title = True
    chart.value_axis.axis_title.text_frame.text = "产值"
    for paragraph in chart.chart_title.text_frame.paragraphs:
        for run in paragraph.runs:
            run.font.size = Pt(20)

    # 2 多级类别（片区 → 工区），类别里有一个纯数字的「2025」不用翻。
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "各工区人数"
    data = CategoryChartData()
    north = data.add_category("北区")
    north.add_sub_category("钢筋班")
    north.add_sub_category("木工班")
    south = data.add_category("南区")
    south.add_sub_category("钢筋班")
    south.add_sub_category("2025")
    data.add_series("在岗人数", (32, 18, 27, 9))
    slide.shapes.add_chart(XL_CHART_TYPE.BAR_CLUSTERED, Inches(1), Inches(1.6), Inches(11), Inches(5.4), data)

    # 3 饼图：数据标签显示类别名。
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "费用构成"
    data = CategoryChartData()
    data.categories = ("人工费", "材料费", "机械费")
    data.add_series("费用", (35, 50, 15))
    chart = slide.shapes.add_chart(XL_CHART_TYPE.PIE, Inches(3), Inches(1.6), Inches(7), Inches(5.4), data).chart
    plot = chart.plots[0]
    plot.has_data_labels = True
    plot.data_labels.show_category_name = True
    plot.data_labels.show_value = False


def _table_workbook() -> bytes:
    """幻灯片 1 图表的数据表，像 PowerPoint 那样在数据上盖一个 Excel 表格。"""
    book = Workbook()
    sheet = book.active
    sheet.title = "Sheet1"
    sheet.append([" ", "计划产值", "实际产值"])
    for quarter, plan, actual in zip(QUARTERS, (120, 150, 180, 210), (110, 160, 175, 0)):
        sheet.append([quarter, plan, actual])
    table = Table(displayName="Table1", ref="A1:C5")
    table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
    sheet.add_table(table)
    data = io.BytesIO()
    book.save(data)
    return data.getvalue()


def _smartart_parts(source: Path) -> dict[str, bytes]:
    """原文件里 SmartArt 的部件（改过文字、放大过），键是本文件里的部件名。"""
    parts: dict[str, bytes] = {}
    with zipfile.ZipFile(source) as package:
        for name in ("layout1.xml", "quickStyle1.xml", "colors1.xml"):
            parts[f"ppt/diagrams/{name}"] = package.read(f"ppt/diagrams/{name}")
        for name in ("data1.xml", "drawing1.xml"):
            root = etree.fromstring(package.read(f"ppt/diagrams/{name}"))
            _replace_text(root)
            if name == "drawing1.xml":
                _scale(root)
            parts[f"ppt/diagrams/{name}"] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        rels = package.read("ppt/diagrams/_rels/data1.xml.rels").replace(b"image1.jpeg", b"image_dgm1.jpeg")
        parts["ppt/diagrams/_rels/data1.xml.rels"] = rels
        parts["ppt/diagrams/_rels/drawing1.xml.rels"] = rels
        parts["ppt/media/image_dgm1.jpeg"] = package.read("ppt/media/image1.jpeg")
    return parts


def _replace_text(root) -> None:
    for paragraph in root.iter(A + "p"):
        runs = paragraph.findall(A + "r")
        if len(runs) != 1:
            continue
        texts = SMARTART_TEXT.get(runs[0].findtext(A + "t") or "")
        if texts is None:
            continue
        run = runs[0]
        if texts == SMARTART_TEXT["def"]:
            bold = run
            bold.find(A + "t").text = texts[0]
            rpr = bold.find(A + "rPr")
            rpr.set("b", "1")
            rpr.attrib.pop("i", None)
            plain = etree.fromstring(etree.tostring(bold))
            plain.find(A + "rPr").set("b", "0")
            plain.find(A + "t").text = texts[1]
            bold.addnext(plain)
            continue
        run.find(A + "t").text = texts[0]
        for extra in texts[1:]:
            copy = etree.fromstring(etree.tostring(paragraph))
            copy.find(A + "r").find(A + "t").text = extra
            paragraph.addnext(copy)


def _scale(root) -> None:
    for element in root.iter(A + "off", A + "ext"):
        for key in ("x", "y", "cx", "cy"):
            if element.get(key) is not None:
                element.set(key, str(int(element.get(key)) * SCALE))
    for element in root.iter(A + "rPr", A + "endParaRPr"):
        if element.get("sz"):
            element.set("sz", str(int(int(element.get("sz")) * FONT_SCALE)))


_DIAGRAM_TYPES = {
    "data1.xml": "application/vnd.openxmlformats-officedocument.drawingml.diagramData+xml",
    "layout1.xml": "application/vnd.openxmlformats-officedocument.drawingml.diagramLayout+xml",
    "quickStyle1.xml": "application/vnd.openxmlformats-officedocument.drawingml.diagramStyle+xml",
    "colors1.xml": "application/vnd.openxmlformats-officedocument.drawingml.diagramColors+xml",
    "drawing1.xml": "application/vnd.ms-office.drawingml.diagramDrawing+xml",
}
_DIAGRAM_RELS = {
    "rId90": ("diagramData", "data1.xml"),
    "rId91": ("diagramLayout", "layout1.xml"),
    "rId92": ("diagramQuickStyle", "quickStyle1.xml"),
    "rId93": ("diagramColors", "colors1.xml"),
}
_FRAME = (
    '<p:graphicFrame xmlns:p="{p}" xmlns:a="{a}"><p:nvGraphicFramePr><p:cNvPr id="90" name="SmartArt 流程"/>'
    "<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>"
    '<p:xfrm><a:off x="{x}" y="1600000"/><a:ext cx="{cx}" cy="{cy}"/></p:xfrm>'
    '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/diagram">'
    '<dgm:relIds xmlns:dgm="{dgm}" xmlns:r="{r}" r:dm="rId90" r:lo="rId91" r:qs="rId92" r:cs="rId93"/>'
    "</a:graphicData></a:graphic></p:graphicFrame>"
)


def _add_smartart(path: Path, source: Path, slide_number: int) -> None:
    parts = _smartart_parts(source)
    slide_part = f"ppt/slides/slide{slide_number}.xml"
    rels_part = f"ppt/slides/_rels/slide{slide_number}.xml.rels"
    temp = path.with_suffix(".tmp")
    with zipfile.ZipFile(path) as package, zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as target:
        for info in package.infolist():
            data = package.read(info.filename)
            if info.filename == "[Content_Types].xml":
                overrides = "".join(
                    f'<Override PartName="/ppt/diagrams/{name}" ContentType="{kind}"/>'
                    for name, kind in _DIAGRAM_TYPES.items()
                )
                if b'Extension="jpeg"' not in data:
                    overrides += '<Default Extension="jpeg" ContentType="image/jpeg"/>'
                data = data.replace(b"</Types>", f"{overrides}</Types>".encode())
            elif info.filename == rels_part:
                rels = "".join(
                    f'<Relationship Id="{rel_id}" Type="{NS["r"]}/{kind}" Target="../diagrams/{name}"/>'
                    for rel_id, (kind, name) in _DIAGRAM_RELS.items()
                )
                rels += (
                    '<Relationship Id="rId6" Type="http://schemas.microsoft.com/office/2007/relationships/'
                    'diagramDrawing" Target="../diagrams/drawing1.xml"/>'
                )
                data = data.replace(b"</Relationships>", f"{rels}</Relationships>".encode())
            elif info.filename == slide_part:
                drawing = etree.fromstring(parts["ppt/diagrams/drawing1.xml"])
                right = max(
                    int(ext.get("cx")) + int(off.get("x"))
                    for off, ext in zip(drawing.iter(A + "off"), drawing.iter(A + "ext"))
                )
                bottom = max(
                    int(ext.get("cy")) + int(off.get("y"))
                    for off, ext in zip(drawing.iter(A + "off"), drawing.iter(A + "ext"))
                )
                frame = _FRAME.format(x=(12192000 - right) // 2, cx=right, cy=bottom, **NS)
                data = re.sub(rb"</p:spTree>", frame.encode() + b"</p:spTree>", data, count=1)
            target.writestr(info, data)
        for name, data in parts.items():
            target.writestr(name, data)
    temp.replace(path)


def build_graphics(path: Path, smartart_source: Path) -> None:
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    _chart_slides(prs)
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "实施流程"
    prs.save(str(path))
    # 幻灯片 1 图表的数据表换成带 Excel 表格的那份。
    temp = path.with_suffix(".tmp")
    with zipfile.ZipFile(path) as package, zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as target:
        slide_rels = etree.fromstring(package.read("ppt/slides/_rels/slide1.xml.rels"))
        chart = next(item.get("Target") for item in slide_rels if item.get("Type").endswith("/chart"))
        chart_part = "ppt/" + chart.removeprefix("../")
        chart_rels = etree.fromstring(package.read(chart_part.replace("charts/", "charts/_rels/") + ".rels"))
        embedded = "ppt/" + next(item.get("Target") for item in chart_rels).removeprefix("../")
        for info in package.infolist():
            data = package.read(info.filename)
            if info.filename == embedded:
                data = _table_workbook()
            target.writestr(info, data)
    temp.replace(path)
    _add_smartart(path, smartart_source, 4)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    build_graphics(HERE / "graphics.pptx", Path(sys.argv[1]))


if __name__ == "__main__":
    main()
