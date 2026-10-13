"""「转成 Word 再翻译」（Beta）：把文字型 PDF 转成 .docx，交给 Word 翻译流程。

一份文件的流程：
1. pdf2docx 把 PDF 转成 .docx（段落、表格、图片、页眉页脚尽量还原）。
2. 修字体：PDF 里嵌的子集字体名（ABCDEF+Arial）、CIDFont+F1 这类名字在
   Word 里不存在，译文会落到默认字体；同时给东亚文字指定一个中文字体，
   否则 Word 用的是转换时记下的西文字体，中文显示成方块或宋体。
3. 用 Word 翻译流程（术语库、翻译记忆、漏译复查都照常）翻成「只保留译文」
   的 .docx，放进 PDF 任务的输出文件夹。

只用于整份都是普通排版页的文档（见 core.pdf_routes）；图纸、扫描件不走这里。
pdf2docx 依赖 PyMuPDF（AGPL）和 OpenCV，只在用到时才导入。
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


def word_route_available() -> bool:
    try:
        _quiet_import_pdf2docx()
    except Exception:  # noqa: BLE001 - missing or broken optional component
        return False
    return True


def _fix_rfonts(tag: str) -> str:
    tag = re.sub(r'w:(ascii|hAnsi|cs)="[A-Z]{6}\+([^"]+)"', r'w:\1="\2"', tag)
    tag = re.sub(r'w:(ascii|hAnsi|cs)="CIDFont\+F\d+"', r'w:\1="Arial"', tag)
    tag = re.sub(r'\s?w:eastAsiaTheme="[^"]*"', "", tag)
    tag = re.sub(r'\s?w:eastAsia="[^"]*"', "", tag)
    return tag.replace("<w:rFonts", f'<w:rFonts w:eastAsia="{EAST_ASIAN_FONT}"', 1)


def normalize_fonts_xml(xml: str) -> str:
    """Only <w:rFonts .../> tags are touched (w:lang also has a w:eastAsia attribute)."""
    return re.sub(r"<w:rFonts\b[^>]*/>", lambda m: _fix_rfonts(m.group(0)), xml)


def normalize_docx_fonts(path: Path) -> None:
    tmp = path.with_suffix(".fonts.docx")
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename in ("word/document.xml", "word/styles.xml") or (
                item.filename.startswith(("word/header", "word/footer")) and item.filename.endswith(".xml")
            ):
                data = normalize_fonts_xml(data.decode("utf-8")).encode("utf-8")
            zout.writestr(item, data)
    tmp.replace(path)


def _quiet_import_pdf2docx():
    """pdf2docx 导入时调 logging.basicConfig()：根 logging 没有处理器时会给它挂一个
    输出到 stderr 的处理器、把级别调到 INFO，此后整个程序（httpx 等）的 INFO 日志都
    会打到 stderr，它自己每页也打一行进度。先挂一个空处理器，basicConfig 就什么都
    不做；只影响原本就没人接收的根日志。"""
    import logging

    if not logging.root.handlers:
        logging.root.addHandler(logging.NullHandler())
    from pdf2docx import Converter

    return Converter


def convert_pdf_to_docx(pdf_path: Path, docx_path: Path, *, should_stop: Callable[[], bool] | None = None) -> None:
    Converter = _quiet_import_pdf2docx()

    docx_path.parent.mkdir(parents=True, exist_ok=True)
    converter = Converter(str(pdf_path))
    try:
        if should_stop and should_stop():
            raise WordRouteStopped()
        converter.convert(str(docx_path), multi_processing=False)
    finally:
        converter.close()
    normalize_docx_fonts(docx_path)


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
) -> WordRouteResult:
    """Convert, translate with the Word pipeline, and place the .docx at final_path."""
    from core.task_runner import DoneMsg, ErrorMsg, LogMsg, ProgressMsg, StoppedMsg
    from core.word_document import scan_word_path
    from core.word_task_runner import WordTaskRunner

    prefix = f"[{file_label}] " if file_label else ""
    work_dir.mkdir(parents=True, exist_ok=True)
    docx = work_dir / f"{pdf_path.stem}.docx"
    try:
        convert_pdf_to_docx(pdf_path, docx, should_stop=should_stop)
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
    runner = WordTaskRunner(items, word_settings, source_root=work_dir, source_lang=source_lang,
                            key_overrides=key_overrides)
    runner.start()
    stop_sent = False
    done = None
    failure = ""
    stopped = False
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
        if isinstance(message, DoneMsg):
            done = message
        elif isinstance(message, ErrorMsg):
            failure = message.message
        elif isinstance(message, StoppedMsg):
            stopped = True
    if done is None:
        return WordRouteResult(ok=False, stopped=stopped or stop_sent,
                               error="任务已停止" if (stopped or stop_sent) else (failure or "Word 翻译没有完成。"))
    outputs = [item for item in done.file_results if item.get("success") and item.get("output")]
    if not outputs or not Path(outputs[0]["output"]).is_file():
        errors = [str(item.get("error") or "") for item in done.file_results]
        return WordRouteResult(ok=False, error=next((e for e in errors if e), "Word 翻译没有生成文件。"))
    final_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(outputs[0]["output"], final_path)
    return WordRouteResult(ok=True, output_path=str(final_path), issues=list(done.issues or []),
                           word_result=dict(outputs[0]))
