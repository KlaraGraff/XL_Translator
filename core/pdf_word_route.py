"""「转成 Word 再翻译」（Beta）：把文字型 PDF 转成 .docx，交给 Word 翻译流程。

一份文件的流程：
1. pdf2docx 把 PDF 转成 .docx（段落、表格、图片、页眉页脚尽量还原）。
2. 修字体：PDF 里嵌的子集字体名（ABCDEF+Arial）、CIDFont+F1 这类名字在
   Word 里不存在，译文会落到默认字体；同时给东亚文字指定一个中文字体，
   否则 Word 用的是转换时记下的西文字体，中文显示成方块或宋体。
3. 用 Word 翻译流程（术语库、翻译记忆、漏译复查都照常）翻成「只保留译文」
   的 .docx，放进 PDF 任务的输出文件夹。

只用于整份都是普通排版页的文档（见 core.pdf_routes）；图纸、扫描件不走这里。
pdf2docx 不随软件分发：它是用户在「插件」页自己连接的可选组件（开源命令行工具，
见 core.pdf2docx_plugin），这里只是调用它的命令行。没连接时这条路不可用，
调用方改用保底方式。
"""
from __future__ import annotations

import re
import shutil
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

LogFn = Callable[[str, str], None]
ProgressFn = Callable[[int, int], None]

# 东亚文字用的字体。Windows 自带微软雅黑；macOS 的 Word/WPS 会自动替换成苹方。
EAST_ASIAN_FONT = "Microsoft YaHei"


class WordRouteStopped(Exception):
    pass


@dataclass
class WordRouteResult:
    ok: bool
    output_path: str = ""
    error: str = ""
    stopped: bool = False
    issues: list[dict] = field(default_factory=list)
    word_result: dict = field(default_factory=dict)
    # 这一段的「记忆库」结果（TaskTermSession.finish），PDF 任务把几段合并后显示。
    terms: dict = field(default_factory=dict)


def word_route_available() -> bool:
    """PDF 处理组件已一键安装，或连接了用户自己装的 pdf2docx。"""
    try:
        return _active_converter() is not None
    except Exception:  # noqa: BLE001 - missing or broken optional component
        return False


def _fix_rfonts(tag: str) -> str:
    tag = re.sub(r'w:(ascii|hAnsi|cs)="[A-Z]{6}\+([^"]+)"', r'w:\1="\2"', tag)
    tag = re.sub(r'w:(ascii|hAnsi|cs)="CIDFont\+F\d+"', r'w:\1="Arial"', tag)
    tag = re.sub(r'\s?w:eastAsiaTheme="[^"]*"', "", tag)
    tag = re.sub(r'\s?w:eastAsia="[^"]*"', "", tag)
    return tag.replace("<w:rFonts", f'<w:rFonts w:eastAsia="{EAST_ASIAN_FONT}"', 1)


def normalize_fonts_xml(xml: str) -> str:
    """Only <w:rFonts .../> tags are touched (w:lang also has a w:eastAsia attribute)."""
    return re.sub(r"<w:rFonts\b[^>]*/>", lambda m: _fix_rfonts(m.group(0)), xml)


def loosen_row_heights_xml(xml: str) -> str:
    """pdf2docx 把表格行高定成「固定值」，正好装下原文那几行。中文换行位置不同、
    字形也高，固定行高会把多出来的一行裁掉；改成「最小值」，原文一样高，译文长了
    行跟着长。"""
    return re.sub(r'(<w:trHeight\b[^>]*?)w:hRule="exact"', r'\1w:hRule="atLeast"', xml)


_EAST_ASIAN_LOCALES = {"zh": "zh-CN", "zh-cn": "zh-CN", "zh-tw": "zh-TW", "zh-hk": "zh-HK", "ja": "ja-JP", "ko": "ko-KR"}


def east_asian_locale(target_lang: str | None) -> str:
    lang = str(target_lang or "").strip().lower().replace("_", "-")
    return _EAST_ASIAN_LOCALES.get(lang) or _EAST_ASIAN_LOCALES.get(lang.split("-")[0], "")


def set_default_east_asian_lang_xml(styles_xml: str, locale: str) -> str:
    """pdf2docx 把文档默认东亚语言写成 en-US，Word 就不按中文的断行规则排：句号、
    逗号会跑到行首。把默认东亚语言改成译文的语言。"""
    if not locale:
        return styles_xml
    match = re.search(r"<w:rPrDefault>\s*<w:rPr>(.*?)</w:rPr>", styles_xml, re.S)
    if not match:
        return styles_xml
    inner = match.group(1)
    if re.search(r"<w:lang\b", inner):
        if re.search(r'<w:lang\b[^>]*w:eastAsia="', inner):
            new_inner = re.sub(r'(<w:lang\b[^>]*?w:eastAsia=")[^"]*"', rf'\g<1>{locale}"', inner, count=1)
        else:
            new_inner = re.sub(r"<w:lang\b", f'<w:lang w:eastAsia="{locale}"', inner, count=1)
    else:
        new_inner = inner + f'<w:lang w:eastAsia="{locale}"/>'
    return styles_xml[: match.start(1)] + new_inner + styles_xml[match.end(1):]


def normalize_docx_fonts(path: Path, east_asian_lang: str = "") -> None:
    tmp = path.with_suffix(".fonts.docx")
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename in ("word/document.xml", "word/styles.xml") or (
                item.filename.startswith(("word/header", "word/footer")) and item.filename.endswith(".xml")
            ):
                text = loosen_row_heights_xml(normalize_fonts_xml(data.decode("utf-8")))
                if item.filename == "word/styles.xml":
                    text = set_default_east_asian_lang_xml(text, east_asian_lang)
                data = text.encode("utf-8")
            zout.writestr(item, data)
    tmp.replace(path)


_SENTENCE_END = tuple(".!?:;。！？：；")
_LIST_START = re.compile(r"^\s*(?:[-•▪●○◦*–—]|\(?\d{1,3}[.)]|\[\d+\]|[a-zA-Z][.)])\s")


def _run_look(run) -> tuple:
    font = run.font
    return (bool(run.bold), bool(run.italic), font.size, font.name)


def _plain_line(paragraph, *, numbered_ok: bool = False) -> bool:
    """A paragraph that is one wrapped line of text: no tabs, breaks, fields, pictures.

    numbered_ok: one tab right after a short number or bullet（「4<tab>正文」）is allowed.
    """
    from docx.oxml.ns import qn

    # 只看正文里的节点：段落属性里的 <w:tabs><w:tab/> 是制表位定义，不是制表符
    tags = [el.tag.rsplit("}", 1)[-1] for child in paragraph._p if child.tag != qn("w:pPr")
            for el in child.iter() if isinstance(el.tag, str)]
    if paragraph.text.strip() == "" or {"br", "drawing", "pict", "fldChar", "instrText",
                                        "footnoteReference", "endnoteReference"} & set(tags):
        return False
    tabs = tags.count("tab")
    if not tabs:
        return True
    return numbered_ok and tabs == 1 and len(paragraph.text.split("\t", 1)[0].strip()) <= 4


def _runs_after_number(paragraph) -> list:
    """「4<tab>正文」这种带编号的行，只比较编号后面的正文字体。"""
    from docx.oxml.ns import qn

    runs = list(paragraph.runs)
    for index, run in enumerate(runs):
        if run._r.find(qn("w:tab")) is not None:
            return runs[index:]
    return runs


def _continues(first, second) -> bool:
    a, b = first.text.rstrip(), second.text.strip()
    if not a or not b or a.endswith(_SENTENCE_END) or _LIST_START.match(b):
        return False
    if not (_plain_line(first, numbered_ok=True) and _plain_line(second)):
        return False
    looks_a = {_run_look(r) for r in _runs_after_number(first) if r.text.strip()}
    looks_b = {_run_look(r) for r in second.runs if r.text.strip()}
    if len(looks_a) != 1 or looks_a != looks_b:
        return False
    # 后一行离前一行太远（隔了一段空白）就是另一段
    gap = second.paragraph_format.space_before
    size = next(iter(looks_a))[2]
    return gap is None or size is None or gap <= size * 1.2


def _merge_paragraph_pairs(paragraphs) -> int:
    merged = 0
    items = list(paragraphs)
    i = 0
    while i < len(items) - 1:
        first, second = items[i], items[i + 1]
        if first._p.getnext() is not second._p or not _continues(first, second):
            i += 1
            continue
        joiner = "" if re.search(r"[\u3000-\u9fff]$", first.text.rstrip()) else " "
        if first.runs:
            first.runs[-1].text = first.runs[-1].text.rstrip() + joiner
        for run in list(second.runs):
            first._p.append(run._r)
        look = _run_look(first.runs[0]) if first.runs else (False, False, None, None)
        heading = look[0] or (look[2] is not None and look[2].pt >= 12)
        if heading and second.alignment != first.alignment and 1 in (first.alignment, second.alignment):
            first.alignment = 1  # 标题：pdf2docx 用缩进模拟的居中，合并后改成真正居中
            first.paragraph_format.left_indent = None
            first.paragraph_format.first_line_indent = None
        second._p.getparent().remove(second._p)
        items.pop(i + 1)
        merged += 1
    return merged


def _join_soft_breaks(paragraph) -> int:
    """段落里的换行符（w:br）多是 PDF 原来的折行位置：前面是没说完的半句（有字母、
    没有句末标点），后面还有字，就换成空格，让 Word 按译文重新折行。分页符、
    只有符号的那种对齐用换行（如一列「-」）保留。"""
    joined = 0
    from docx.oxml.ns import qn

    for br in list(paragraph._p.iter(qn("w:br"))):
        if br.get(qn("w:type")) not in (None, "textWrapping"):
            continue
        before, after = [], []
        seen = False
        for node in paragraph._p.iter(qn("w:t"), qn("w:br")):
            if node is br:
                seen = True
                continue
            if node.tag == qn("w:br"):
                if seen:
                    break
                before.clear()
                continue
            (after if seen else before).append(node.text or "")
        raw_head, raw_tail = "".join(before), "".join(after)
        head, tail = raw_head.rstrip(), raw_tail.strip()
        if not tail or not head or not re.search(r"[^\W\d_]", head) or head.endswith(_SENTENCE_END):
            continue
        run = br.getparent()
        t = run.makeelement(qn("w:t"), {})
        # 折行前后本来就带空格（pdf2docx 常见）时不再加，免得原文里出现两个空格
        t.text = "" if raw_head[-1:].isspace() or raw_tail[:1].isspace() else " "
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        run.replace(br, t)
        joined += 1
    return joined


# 相邻两段文字之间补空格的条件：前一段以西文字母、数字或句读结尾，后一段以西文字母、
# 数字或左括号、左引号开头。中日韩文字不在里面，不会被插进空格。
_SPACE_BEFORE = re.compile(r"[A-Za-z0-9.,;:!?)\]’'\"”%]$")
_SPACE_AFTER = re.compile(r"^[A-Za-z0-9(\[“‘\"]")
_TOKEN_END = re.compile(r"[\w’'-]+$")
_TOKEN_START = re.compile(r"^[\w’'-]+")


def pdf_words(pdf_path: Path) -> set[str]:
    """PDF 里的词（PyMuPDF 按字距判断词界，不依赖空格字符）：用来认出被拆成两段的同一个词。"""
    try:
        from core.pdf_component import require_fitz

        fitz = require_fitz()

        with fitz.open(pdf_path) as doc:
            return {match.group(0) for page in doc for word in page.get_text("words")
                    for match in [_TOKEN_START.search(word[4].strip("([“‘\"")) ] if match}
    except Exception:  # noqa: BLE001 - without the word list we only add spaces where both sides are words
        return set()


def _restore_spaces_in(paragraph, words: set[str]) -> int:
    """pdf2docx 会丢掉只有空格的文字片段。不少 PDF（LaTeX、troff 排的论文）词和词之间
    的空格是单独一段，转出来就成了「shell’ssyntax」「Anumber」，读着别扭，翻译也容易
    失败。同一段里前后两段文字直接相接、两边都是西文时补回一个空格；换行、制表符、
    上下标（脚注号）两侧不补；两段拼起来正好是 PDF 里的一个词（「ev」+「al」）也不补。"""
    from docx.oxml.ns import qn

    added = 0
    weight: dict[bytes, int] = {}
    for t in paragraph._p.iter(qn("w:t")):
        key = _run_look_xml(t.getparent())
        weight[key] = weight.get(key, 0) + len(t.text or "")
    base = max(weight, key=weight.get) if weight else b""
    prev = None  # 上一个 w:t
    for node in paragraph._p.iter(qn("w:t"), qn("w:br"), qn("w:tab"), qn("w:cr")):
        if node.tag != qn("w:t"):
            prev = None
            continue
        text = node.text or ""
        if not text:
            continue
        run = node.getparent()
        rpr = run.find(qn("w:rPr")) if run is not None else None
        script = rpr is not None and rpr.find(qn("w:vertAlign")) is not None
        if (prev is not None and not script and prev.getparent() is not run
                and _SPACE_BEFORE.search(prev.text or "") and _SPACE_AFTER.match(text)
                and not _split_word(prev.text or "", text, words)):
            # 两段格式不同时，空格补在段落主体格式那一侧，免得落进加粗、链接这类格式
            # 标记里（译文里成了「<g1> STOP</g1>」）。
            target = node
            if _run_look_xml(run) != _run_look_xml(prev.getparent()) and _run_look_xml(prev.getparent()) == base:
                target = prev
            if target is node:
                node.text = " " + text
            else:
                prev.text = (prev.text or "") + " "
            target.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            added += 1
        prev = None if script else node
    return added


def _run_look_xml(run) -> bytes:
    from docx.oxml.ns import qn
    from lxml import etree

    rpr = run.find(qn("w:rPr")) if run is not None else None
    return b"" if rpr is None else etree.tostring(rpr)


def _split_word(before: str, after: str, words: set[str]) -> bool:
    left, right = _TOKEN_END.search(before), _TOKEN_START.match(after)
    return bool(left and right and left.group(0) + right.group(0) in words)


def restore_word_spaces(path: Path, words: set[str] | None = None) -> int:
    """words: 源 PDF 的词表（pdf_words），拼起来是一个词的两段不补空格。"""
    from docx import Document

    words = words or set()
    doc = Document(str(path))
    count = sum(_restore_spaces_in(p, words) for p in doc.paragraphs)
    for table in _all_tables(doc.tables):
        for row in table.rows:
            for cell in row.cells:
                count += sum(_restore_spaces_in(p, words) for p in cell.paragraphs)
    if count:
        doc.save(str(path))
    return count


def merge_wrapped_lines(path: Path) -> int:
    """pdf2docx 有时把同一段的几行（多见于标题、图注、居中的作者单位）拆成几个段落。
    按段翻译时每一行都成了半句话，译文出现「……」。相邻两段字体完全相同、前一段
    没以句末标点结束、后一段不是列表项、中间没有空行，就并成一段。"""
    from docx import Document

    doc = Document(str(path))
    count = _merge_paragraph_pairs(doc.paragraphs)
    count += sum(_join_soft_breaks(p) for p in doc.paragraphs)
    for table in _all_tables(doc.tables):
        for row in table.rows:
            for cell in row.cells:
                count += _merge_paragraph_pairs(cell.paragraphs)
                count += sum(_join_soft_breaks(p) for p in cell.paragraphs)
    if count:
        doc.save(str(path))
    return count


def _all_tables(tables):
    for table in tables:
        yield table
        for row in table.rows:
            for cell in row.cells:
                yield from _all_tables(cell.tables)


def _active_converter() -> tuple[str, Path] | None:
    from core.pdf_component import active_converter

    return active_converter()


def _conversion_timeout(pdf_path: Path) -> float:
    """pdf2docx 一页大约一秒；按页数给足时间，卡死的转换也不会一直挂着。"""
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(str(pdf_path))
        try:
            pages = len(doc)
        finally:
            doc.close()
    except Exception:  # noqa: BLE001 - unreadable PDF: let the converter report it
        pages = 0
    return float(max(300, 30 * pages))


def convert_pdf_to_docx(
    pdf_path: Path,
    docx_path: Path,
    *,
    should_stop: Callable[[], bool] | None = None,
    target_lang: str = "",
) -> None:
    from core.pdf2docx_plugin import Pdf2DocxPluginError, convert_with_command, convert_with_plugin
    from core.pdf_component import SOURCE_MANAGED, managed_convert_command

    active = _active_converter()
    if active is None:
        raise Pdf2DocxPluginError("PDF component (pdf2docx) is not installed")
    if should_stop and should_stop():
        raise WordRouteStopped()
    source, where = active
    timeout = _conversion_timeout(pdf_path)
    if source == SOURCE_MANAGED:
        # 一键安装的组件：在单独的子进程里跑 pdf2docx，NumPy、OpenCV 不进翻译引擎。
        convert_with_command(managed_convert_command(where, pdf_path, docx_path), docx_path, timeout)
    else:
        convert_with_plugin(where, pdf_path, docx_path, timeout)
    normalize_docx_fonts(docx_path, east_asian_locale(target_lang))
    restore_word_spaces(docx_path, pdf_words(pdf_path))
    merge_wrapped_lines(docx_path)


def translate_pdf_via_word(
    pdf_path: Path,
    work_dir: Path,
    final_path: Path,
    settings,
    *,
    source_lang: str,
    should_stop: Callable[[], bool],
    log: LogFn,
    progress: ProgressFn | None = None,
    file_label: str = "",
    key_overrides: dict[str, str] | None = None,
    term_file_paths: list[Path] | None = None,
) -> WordRouteResult:
    """Convert, translate with the Word pipeline, and place the .docx at final_path."""
    from core.task_runner import DoneMsg, ErrorMsg, LogMsg, ProgressMsg, StoppedMsg
    from core.word_document import scan_word_path
    from core.word_task_runner import WordTaskRunner

    prefix = f"[{file_label}] " if file_label else ""
    work_dir.mkdir(parents=True, exist_ok=True)
    docx = work_dir / f"{pdf_path.stem}.docx"
    try:
        convert_pdf_to_docx(pdf_path, docx, should_stop=should_stop,
                            target_lang=settings.pdf.target_lang or settings.target_lang)
    except WordRouteStopped:
        return WordRouteResult(ok=False, stopped=True, error="任务已停止")
    except Exception as exc:  # noqa: BLE001 - conversion failure is reported per file
        logger.debug(f"[PDF] 转 Word 失败：{exc!r}")
        return WordRouteResult(ok=False, error="转成 Word 失败，这份文件可能不是普通排版的文字文档。")
    log("INFO", f"{prefix}已转成 Word，开始按 Word 流程翻译。")

    word_settings = settings.model_copy(deep=True)
    word_settings.target_lang = settings.pdf.target_lang or settings.target_lang
    word_settings.word_output.use_custom_output_dir = True
    word_settings.word_output.custom_output_dir = str(work_dir / "out")
    word_settings.word_output.output_translation_only = True
    word_settings.word_output.translate_output_filename = False
    (work_dir / "out").mkdir(parents=True, exist_ok=True)
    items = scan_word_path(docx)
    if not items:
        return WordRouteResult(ok=False, error="转出来的 Word 里没有可翻译的文字。")
    # 用 PDF 任务开始时拿到的密钥快照，和整个任务用同一套连接
    # 记忆库词条的「只在…中生效」按原 PDF 的文件名和文件夹判断，不按临时转出的 Word。
    runner = WordTaskRunner(items, word_settings, source_root=work_dir, source_lang=source_lang,
                            key_overrides=key_overrides,
                            term_file_paths=term_file_paths or [pdf_path])
    runner.start()
    stop_sent = False
    done = None
    failure = ""
    stopped = False
    terms: dict = {}
    while runner.needs_poll():
        if not stop_sent and should_stop():
            runner.stop()
            stop_sent = True
        message = runner.get_message(timeout=0.2)
        if message is None:
            continue
        if isinstance(message, LogMsg):
            if message.level in ("WARN", "ERROR"):
                log(message.level, f"{prefix}{message.message}")
            continue
        if isinstance(message, ProgressMsg):
            if progress is not None and message.step_total:
                progress(int(message.step_done), int(message.step_total))
            continue
        if isinstance(message, (DoneMsg, ErrorMsg, StoppedMsg)):
            terms = dict(getattr(message, "terms", None) or {})
        if isinstance(message, DoneMsg):
            done = message
        elif isinstance(message, ErrorMsg):
            failure = message.message
        elif isinstance(message, StoppedMsg):
            stopped = True
    if done is None:
        return WordRouteResult(ok=False, stopped=stopped or stop_sent, terms=terms,
                               error="任务已停止" if (stopped or stop_sent) else (failure or "Word 翻译没有完成。"))
    outputs = [item for item in done.file_results if item.get("success") and item.get("output")]
    if not outputs or not Path(outputs[0]["output"]).is_file():
        errors = [str(item.get("error") or "") for item in done.file_results]
        return WordRouteResult(ok=False, terms=terms, error=next((e for e in errors if e), "Word 翻译没有生成文件。"))
    final_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(outputs[0]["output"], final_path)
    return WordRouteResult(ok=True, output_path=str(final_path), issues=list(done.issues or []),
                           word_result=dict(outputs[0]), terms=terms)


# --------------------------------------------------------------------------
# 混合文件里的正文段：译好的 Word 要转回 PDF，才能和扫描页、图纸页按原页序拼成一份。
# Word 转 PDF 要本机的 Microsoft Word（软件转 .doc 时已经在用同一条自动化通道），
# 没有 Word 时退到 LibreOffice。两样都没有，正文页就不走转 Word（见调用方）。
# --------------------------------------------------------------------------

WORD_PDF_FORMAT = 17  # wdFormatPDF


def cjk_text_visible(pdf_path: Path, max_pages: int = 3) -> bool:
    """转回的 PDF 里中文看不看得见。本机缺中文字体时（实测 macOS 上的 LibreOffice）
    中文照样写进了文字层，却画成空白；按中文字所在区域的墨迹判断。没有中文时算看得见。"""
    import statistics

    from core.pdf_component import PdfComponentMissing, require_fitz

    try:
        fitz = require_fitz()
    except PdfComponentMissing:
        return True  # 查不了就不拦：转回的 PDF 照常拼入

    values: list[float] = []
    with fitz.open(pdf_path) as doc:
        for page in list(doc)[:max_pages]:
            spans = [span for block in page.get_text("dict")["blocks"] for line in block.get("lines", [])
                     for span in line["spans"]
                     if sum("\u3000" <= ch <= "\u9fff" for ch in span["text"]) >= 2]
            if not spans:
                continue
            pix = page.get_pixmap(dpi=72, colorspace=fitz.csGRAY)
            for span in spans[:40]:
                rect = fitz.IRect(fitz.Rect(span["bbox"])) & pix.irect
                if rect.is_empty:
                    continue
                dark = total = 0
                for y in range(rect.y0, rect.y1):
                    row = pix.samples[y * pix.stride + rect.x0: y * pix.stride + rect.x1]
                    dark += sum(1 for value in row if value < 160)
                    total += len(row)
                values.append(dark / total if total else 0.0)
    return not values or statistics.median(values) >= 0.03


class OfficePdfError(Exception):
    pass


def office_pdf_engine() -> tuple[str, str]:
    """("word" | "libreoffice" | "", message). Never starts an application."""
    from core.word_converter import _find_soffice, get_local_word_automation_availability

    ok, message = get_local_word_automation_availability()
    if ok:
        return "word", message
    if _find_soffice() is not None:
        return "libreoffice", "未检测到 Microsoft Word，改用 LibreOffice 把 Word 转回 PDF。"
    return "", "本机没有 Microsoft Word，也没有 LibreOffice，译好的 Word 没法转回 PDF。"


def docx_to_pdf(docx_path: Path, pdf_path: Path, *, timeout: int = 300) -> str:
    """Render a .docx to PDF with Microsoft Word (or LibreOffice); returns the engine used.

    先把文件复制到一个短路径、固定的临时文件夹里，用唯一的文件名交给 Word：
    - 过程文件夹的路径很长，macOS 版 Word 另存超过约 255 个字符的路径会直接报错
      （Windows 也有 260 的上限）；
    - macOS 版 Word 在沙盒里，每遇到一个没授权过的文件夹都要弹窗请用户授权，所以
      文件夹必须固定，和软件转 .doc 时用的是同一个（word_converter._get_temp_docx_path），
      授权一次就够；
    - 唯一的文件名避免撞上 Word 里已经开着的同名文档。
    """
    import platform
    import subprocess
    import tempfile
    import uuid

    from core.word_converter import _find_soffice, get_local_word_automation_availability

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.unlink(missing_ok=True)
    errors: list[str] = []
    stage = Path(tempfile.gettempdir()) / "word_translator_temp"
    stage.mkdir(parents=True, exist_ok=True)
    staged_docx = stage / f"pdfseg_{uuid.uuid4().hex[:8]}.docx"
    staged_pdf = stage / f"{staged_docx.stem}.pdf"
    out_dir = stage / f"{staged_docx.stem}_lo"
    try:
        shutil.copy2(docx_path, staged_docx)
        word_ok, _message = get_local_word_automation_availability()
        if word_ok:
            try:
                if platform.system() == "Windows":
                    _docx_to_pdf_windows_word(staged_docx, staged_pdf)
                else:
                    _docx_to_pdf_macos_word(staged_docx, staged_pdf, timeout)
                if staged_pdf.is_file() and staged_pdf.stat().st_size > 0:
                    if cjk_text_visible(staged_pdf):
                        shutil.move(str(staged_pdf), str(pdf_path))
                        return "word"
                    errors.append("Word 转出的 PDF 里中文显示不出来")
                else:
                    errors.append("Word 没有生成 PDF")
            except Exception as exc:  # noqa: BLE001 - try LibreOffice next
                logger.debug(f"[PDF] Word 转 PDF 失败：{exc!r}")
                errors.append(f"Word：{exc}")
        soffice = _find_soffice()
        if soffice is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            try:
                # 独立的临时配置目录：用户开着 LibreOffice 时，不带它的命令会转交给那个
                # 正在运行的实例，结果什么都不生成。
                profile = out_dir / "lo_profile"
                subprocess.run(
                    [str(soffice), "--headless", "--norestore", f"-env:UserInstallation={profile.as_uri()}",
                     "--convert-to", "pdf", "--outdir", str(out_dir), str(staged_docx)],
                    capture_output=True, timeout=timeout, check=False,
                )
                produced = out_dir / f"{staged_docx.stem}.pdf"
                if produced.is_file() and produced.stat().st_size > 0:
                    if cjk_text_visible(produced):
                        shutil.move(str(produced), str(pdf_path))
                        return "libreoffice"
                    errors.append("LibreOffice 转出的 PDF 里中文显示不出来（缺少中文字体）")
                else:
                    errors.append("LibreOffice 没有生成 PDF")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"LibreOffice：{exc}")
    finally:
        for leftover in (staged_docx, staged_pdf):
            leftover.unlink(missing_ok=True)
        shutil.rmtree(out_dir, ignore_errors=True)
    raise OfficePdfError("；".join(errors) or "本机没有可以把 Word 转成 PDF 的程序。")


def _docx_to_pdf_macos_word(docx_path: Path, pdf_path: Path, timeout: int) -> None:
    import subprocess

    # 只动自己打开的这一份：按文件名找到它，用完关掉；用户在 Word 里开着的其他文档不碰。
    script = """
on run argv
    set inputPath to POSIX file (item 1 of argv)
    set outputPath to (item 2 of argv)
    set docName to (item 3 of argv)
    tell application "Microsoft Word"
        try
            set display alerts to false
        end try
        open inputPath
        set theDoc to missing value
        set n to 0
        repeat while theDoc is missing value and n < 60
            repeat with i from 1 to (count documents)
                if name of document i is docName then set theDoc to document i
            end repeat
            if theDoc is missing value then
                delay 1
                set n to n + 1
            end if
        end repeat
        if theDoc is missing value then error "Word 没有打开这份文件"
        -- 文件刚打开时 Word 可能还在排版，这时另存会报「不认识 save as」；等一下再试。
        set lastErr to ""
        set didExport to false
        repeat with attempt from 1 to 3
            delay 2
            try
                save as theDoc file name outputPath file format format PDF
                set didExport to true
                exit repeat
            on error errMsg
                set lastErr to errMsg
            end try
        end repeat
        try
            close theDoc saving no
        end try
        if not didExport then error lastErr
    end tell
end run
"""
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script, str(docx_path), str(pdf_path), docx_path.name],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if result.returncode != 0:
        raise OfficePdfError((result.stderr or result.stdout or "Word 自动化失败").strip())


def _docx_to_pdf_windows_word(docx_path: Path, pdf_path: Path) -> None:
    import pythoncom
    import win32com.client

    word = None
    document = None
    pythoncom.CoInitialize()
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        document = word.Documents.Open(str(docx_path), ReadOnly=True, AddToRecentFiles=False,
                                       ConfirmConversions=False)
        document.SaveAs2(str(pdf_path), FileFormat=WORD_PDF_FORMAT)
    finally:
        if document is not None:
            try:
                document.Close(False)
            except Exception:  # noqa: BLE001
                pass
        if word is not None:
            try:
                word.Quit()
            except Exception:  # noqa: BLE001
                pass
        pythoncom.CoUninitialize()
