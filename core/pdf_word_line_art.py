"""PDF 转 Word 后，一行一行重排过的格子里那些画线用的小表格，换成原文那几条线的图（反馈 #28 讲义第 33 页）。

讲义的证明表（Fitch 证明）每一层子证明左边有一条竖线，假设那一步下面有一条短横线。pdf2docx 把这些线
做成格子里的小表格：一张只有边框、没有字的表，竖线是左边框，短横线是下边框，按 Word 的排版一张接一张
往下排，各占几十到两百多磅高。证明表按原文一行一段重排以后（core.pdf_word_lines），每一行的位置按原文
定，这些小表格却还在排版的流里：被挤到格子最下面，多出一截空竖线，格子装不下，右边那一列也跟着不见了。

做法：重排过的格子里，没有字、没有图的小表格去掉；原 PDF 上这几张小表格那一带的细线（竖线、短横线，
细到 pdf2docx 会当成边框的）照原样画到一张透明的图上，按页面定位放在字的上面（格子有底色，放在字下面
会被底色盖住）。线的位置、长短、粗细都和原文一样；线在字和字之间，不挡字。
小表格里有字或图片的，这一格不重排（core.pdf_word_lines 先问 bare_tables）。
"""
from __future__ import annotations

import io

from loguru import logger

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_EMU_PER_POINT = 12700

#: 比这细（磅）的线、细长方块算边框一类的线，和 pdf2docx 认边框的上限一样（max_border_width）。
_THIN = 6.0

#: 找线时左右、上下多放这么几磅（线有粗细，小表格的位置是 pdf2docx 估的）。
_SLACK = 2.0

#: 画图的倍数（72 dpi 的几倍）：线只有零点几磅，放大一些边缘才不糊。
_ZOOM = 4.0


def bare_tables(tc) -> list | None:
    """格子里直接放着的小表格，都只有边框（没有字、没有图）时返回它们；有一张带字或图的返回 None。"""
    found = []
    for child in tc:
        if child.tag != f"{_W}tbl":
            continue
        if any((node.text or "").strip() for node in child.iter(f"{_W}t")) or next(
                child.iter(f"{_W}drawing", f"{_W}pict", f"{_W}object"), None) is not None:
            return None
        found.append(child)
    return found


def _twips(node, name: str) -> float:
    try:
        return float(node.get(f"{_W}{name}") or 0) / 20 if node is not None else 0.0
    except ValueError:
        return 0.0


def spans(tables: list) -> list[tuple[float, float]]:
    """每张小表格相对格子文字左边的左右范围（磅）：表格缩进到缩进加各列宽。"""
    found = []
    for table in tables:
        left = _twips(table.find(f"{_W}tblPr/{_W}tblInd"), "w")
        width = sum(_twips(column, "w") for column in table.findall(f"{_W}tblGrid/{_W}gridCol"))
        found.append((left, left + width))
    return found


def thin_paths(page, left: float, right: float, top: float, bottom: float) -> list[dict]:
    """原 PDF 这一页落在左右 left–right 之内、上下碰到 top–bottom 的细线（整条留下）。
    格子的底色、方框这些大块的、宽过这一带的不算。"""
    found = []
    for path in page.get_drawings():
        rect = path["rect"]
        thick = max(path.get("width") or 0.0, min(rect.width, rect.height))
        if thick > _THIN:
            continue
        if rect.x0 < left - _SLACK or rect.x1 > right + _SLACK:
            continue
        if rect.y1 < top - _SLACK or rect.y0 > bottom + _SLACK:
            continue
        found.append(path)
    return found


def render(paths: list[dict], width: float, height: float, fitz) -> tuple[bytes, tuple] | None:
    """把这几条线照原样画到一张透明的图上：返回 (PNG, 图在页上的位置 x0, y0, x1, y1)。"""
    if not paths:
        return None
    try:
        canvas = fitz.open()
        page = canvas.new_page(width=width, height=height)
        shape = page.new_shape()
        x0 = y0 = float("inf")
        x1 = y1 = float("-inf")
        for path in paths:
            for item in path["items"]:
                kind = item[0]
                if kind == "l":
                    shape.draw_line(item[1], item[2])
                elif kind == "re":
                    shape.draw_rect(item[1])
                elif kind == "qu":
                    shape.draw_quad(item[1])
                elif kind == "c":
                    shape.draw_bezier(item[1], item[2], item[3], item[4])
            stroke = path.get("width") or 0.0
            shape.finish(color=path.get("color"), fill=path.get("fill"), width=stroke,
                         closePath=bool(path.get("closePath")), even_odd=bool(path.get("even_odd")),
                         dashes=path.get("dashes") or None,
                         fill_opacity=path.get("fill_opacity") or 1, stroke_opacity=path.get("stroke_opacity") or 1)
            rect = path["rect"]
            x0, y0 = min(x0, rect.x0 - stroke), min(y0, rect.y0 - stroke)
            x1, y1 = max(x1, rect.x1 + stroke), max(y1, rect.y1 + stroke)
        shape.commit()
        clip = fitz.Rect(max(0.0, x0 - 1), max(0.0, y0 - 1), min(width, x1 + 1), min(height, y1 + 1))
        png = page.get_pixmap(clip=clip, matrix=fitz.Matrix(_ZOOM, _ZOOM), alpha=True).tobytes("png")
        canvas.close()
        return png, (clip.x0, clip.y0, clip.x1, clip.y1)
    except Exception as exc:  # noqa: BLE001 - no picture: the lines are left out, the text is still right
        logger.info(f"[PDF] 画不出原文那几条线：{exc!r}")
        return None


def _next_id(body) -> int:
    ids = [0]
    for node in body.iter(f"{{{_WP_NS}}}docPr"):
        try:
            ids.append(int(node.get("id") or 0))
        except ValueError:
            continue
    return max(ids) + 1


def place(document, paragraph, png: bytes, where: tuple) -> None:
    """把图放进 paragraph：浮在字上面，按页面定位到 where（x0, y0, x1, y1，PDF 坐标，磅）。
    和 pdf2docx 自己放的图一样，挂在表格外面的正文段上、上下左右都按页面定位：挂在格子里时，
    Word 和新版 LibreOffice 会从格子量起，整张图往下错开一大截（讲义第 33 页错了七行）。"""
    from docx.oxml import parse_xml

    r_id, _image = document.part.get_or_add_image(io.BytesIO(png))
    number = _next_id(document.element.body)
    left, top = round(where[0] * _EMU_PER_POINT), round(where[1] * _EMU_PER_POINT)
    width = round((where[2] - where[0]) * _EMU_PER_POINT)
    height = round((where[3] - where[1]) * _EMU_PER_POINT)
    run = parse_xml(
        f'<w:r xmlns:w="{_W[1:-1]}" xmlns:wp="{_WP_NS}" xmlns:a="{_A_NS}" xmlns:pic="{_PIC_NS}" '
        f'xmlns:r="{_R_NS}"><w:drawing>'
        f'<wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="{251659264 + number}" '
        f'behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1"><wp:simplePos x="0" y="0"/>'
        f'<wp:positionH relativeFrom="page"><wp:posOffset>{left}</wp:posOffset></wp:positionH>'
        f'<wp:positionV relativeFrom="page"><wp:posOffset>{top}</wp:posOffset></wp:positionV>'
        f'<wp:extent cx="{width}" cy="{height}"/><wp:effectExtent l="0" t="0" r="0" b="0"/><wp:wrapNone/>'
        f'<wp:docPr id="{number}" name="Lines {number}"/>'
        f'<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        f'<a:graphic><a:graphicData uri="{_PIC_NS}"><pic:pic>'
        f'<pic:nvPicPr><pic:cNvPr id="0" name="lines.png"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{r_id}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{width}" cy="{height}"/></a:xfrm>'
        f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic>'
        f'</wp:anchor></w:drawing></w:r>')
    paragraph.append(run)


def anchor_paragraph(group: list, tc):
    """图挂在哪一段：这一页上、这张表前面最近的正文段（不在表格里），没有就后面最近的；
    这一页都没有正文段时挂在格子第一段。"""
    top = tc
    while top.getparent() is not None and not any(top is element for element in group):
        top = top.getparent()
    index = next((number for number, element in enumerate(group) if element is top), None)
    if index is not None:
        for element in [*reversed(group[:index]), *group[index + 1:]]:
            if element.tag == f"{_W}p":
                return element
    return tc.find(f"{_W}p")


def replace_bars(document, group: list, page, tc, tables: list, origin_left: float,
                 top: float, bottom: float, fitz) -> bool:
    """去掉格子里画线用的小表格，换成原文那一带的细线图。返回画上了没有（小表格总是去掉）。
    origin_left 是格子文字左边在 PDF 上的位置，top、bottom 是格子里的字在 PDF 上的上下范围。"""
    ranges = spans(tables)
    for table in tables:
        tc.remove(table)
    if tc.find(f"{_W}p") is None or tc[-1].tag != f"{_W}p":
        tc.append(tc.makeelement(f"{_W}p", {}))
    if not ranges:
        return False
    left = origin_left + min(start for start, _end in ranges)
    right = origin_left + max(end for _start, end in ranges)
    drawn = render(thin_paths(page, left, right, top, bottom), page.rect.width, page.rect.height, fitz)
    if drawn is None:
        return False
    place(document, anchor_paragraph(group, tc), *drawn)
    return True
