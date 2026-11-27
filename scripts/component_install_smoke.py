"""真机检查「PDF 处理组件」「文字识别组件」（Tesseract 和中文识别模型）「段落重排组件」（BabelDOC）
一键安装：真下载、核对、解压、试运行，再卸载。

在 macOS / Windows 上跑（PR checks 手动触发时由 CI 跑，也可以在本机跑）：

    python scripts/component_install_smoke.py [pdf] [ocr] [ppocr] [babeldoc]

用临时的应用数据目录（设了 TRANSLATOR_APP_DATA_DIR 时用它），不碰本机已装的组件和真实设置。
都成功时退出 0。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _progress(name: str):
    last = [0.0]

    def report(phase: str, done: int, total: int) -> None:
        now = time.monotonic()
        if phase != "download" or now - last[0] > 5:
            last[0] = now
            amount = f"{done / 1048576:.1f} / {total / 1048576:.1f} MB" if phase == "download" else f"{done}/{total}"
            print(f"  [{name}] {phase}: {amount}", flush=True)

    return report


def _pdf() -> bool:
    from core import pdf_component
    from core import pdf_component_install as installer

    print(f"[pdf] platform: {installer.platform_key() or 'unsupported'}, {installer.download_size() / 1048576:.1f} MB")
    started = time.monotonic()
    try:
        info = installer.install(progress=_progress("pdf"))
    except installer.ComponentInstallError as exc:
        print(f"[pdf] INSTALL FAILED: {exc}")
        return False
    print(f"[pdf] installed in {time.monotonic() - started:.0f}s: {info}")
    ok, version, detail = pdf_component.probe_in_subprocess(Path(info["site_dir"]), convert=True)
    print(f"[pdf] probe with a PDF-to-Word conversion: ok={ok} version={version} {detail}")
    installer.uninstall()
    return ok and installer.installed_site_dir() is None


def _ocr() -> bool:
    from core import ocr_component_install as installer

    print(f"[ocr] platform: {installer.platform_key() or 'unsupported'}, {installer.download_size() / 1048576:.1f} MB")
    started = time.monotonic()
    try:
        info = installer.install(progress=_progress("ocr"))
    except installer.ComponentInstallError as exc:
        print(f"[ocr] INSTALL FAILED: {exc}")
        return False
    print(f"[ocr] installed in {time.monotonic() - started:.0f}s: {info}")
    # 再按 PDF 原位替换的调用方式认一行字（图片走 stdin，语言数据给相对路径）。
    from PIL import Image, ImageDraw, ImageFont

    from core.pdf_vector import extract

    image = Image.new("L", (900, 160), 255)
    ImageDraw.Draw(image).text((20, 40), "Plan de masse 2026", fill=0, font=ImageFont.load_default(size=56))
    words = [word["text"] for word in extract._tess(image, "fra+eng", info["path"])]
    print(f"[ocr] recognised: {words}")
    installer.uninstall()
    return "masse" in words and installer.installed_executable() is None


def _ppocr() -> bool:
    from core import pdf_component_install as pdf_installer
    from core import ppocr_component
    from core import ppocr_component_install as installer

    print(f"[ppocr] platform: {installer.platform_key() or 'unsupported'}, {installer.download_size() / 1048576:.1f} MB")
    # NumPy、OpenCV 用「PDF 处理组件」里的那份
    own_pdf = pdf_installer.installed_site_dir() is None
    try:
        if own_pdf:
            pdf_installer.install(progress=_progress("pdf"))
        started = time.monotonic()
        info = installer.install(progress=_progress("ppocr"))
    except (pdf_installer.ComponentInstallError, installer.ComponentInstallError) as exc:
        print(f"[ppocr] INSTALL FAILED: {exc}")
        return False
    print(f"[ppocr] installed in {time.monotonic() - started:.0f}s: {info}")
    # 再按原位替换的调用方式读一行中文和法文（识别子进程、按块识别）
    from PIL import Image, ImageDraw, ImageFont

    from core.pdf_vector.fonts import find_cjk_font

    font_path = find_cjk_font()
    text = "低压配电间 Salle 2026" if font_path else "Salle de repos 2026"
    font = ImageFont.truetype(str(font_path), 56) if font_path else ImageFont.load_default(size=56)
    image = Image.new("L", (1400, 2400), 255)
    ImageDraw.Draw(image).text((60, 1000), text, fill=0, font=font)
    reader = ppocr_component.make_reader()
    started = time.monotonic()
    try:
        rows = reader(image) if reader is not None else []
    except ppocr_component.OcrReaderError as exc:
        print(f"[ppocr] READ FAILED: {exc}")
        rows = []
    read = " ".join(row["source_text"] for row in rows)
    print(f"[ppocr] font: {font_path}; read in {time.monotonic() - started:.1f}s: {read!r}")
    ppocr_component.shutdown_reader()
    installer.uninstall()
    if own_pdf:
        pdf_installer.uninstall()
    expected = ["配电", "Salle"] if font_path else ["Salle", "repos"]
    return all(word in read for word in expected) and installer.installed_dirs() is None


def _babeldoc() -> bool:
    from core import babeldoc_component
    from core import babeldoc_component_install as installer
    from core import pdf_component_install as pdf_installer

    print(f"[babeldoc] platform: {installer.platform_key() or 'unsupported'}, "
          f"{installer.download_size() / 1048576:.1f} MB")
    # NumPy、OpenCV、PyMuPDF 用「PDF 处理组件」里的那份
    own_pdf = pdf_installer.installed_site_dir() is None
    try:
        if own_pdf:
            pdf_installer.install(progress=_progress("pdf"))
        started = time.monotonic()
        # 安装的最后一步已经在子进程里真排了一页（版面模型、字体、写 PDF 都加载一遍）
        info = installer.install(progress=_progress("babeldoc"))
    except (pdf_installer.ComponentInstallError, installer.ComponentInstallError) as exc:
        print(f"[babeldoc] INSTALL FAILED: {exc}")
        return False
    print(f"[babeldoc] installed in {time.monotonic() - started:.0f}s: {info}")
    # 再按 PDF 任务的调用方式翻两页：要译文的请求经 stdin/stdout 交回这边，这里替模型按 BabelDOC
    # 要的格式作答（每段译文 = 「TR 」+ 原文）。
    from core.pdf_component import forget_failures, require_fitz

    forget_failures()
    fitz = require_fitz()
    work = Path(os.environ["TRANSLATOR_APP_DATA_DIR"]) / "babeldoc-smoke"
    work.mkdir(parents=True, exist_ok=True)
    source = work / "source.pdf"
    doc = fitz.open()
    for number in (1, 2):
        page = doc.new_page(width=595, height=842)
        page.insert_textbox(fitz.Rect(72, 90, 523, 400),
                            f"Page {number}. The applicant must present a valid passport before 14/6/24. "
                            "Section 3 explains how records dated 20120826 are checked.", fontsize=12, fontname="helv")
    doc.save(str(source))
    doc.close()
    asked = {"llm": 0, "plain": 0}

    def translate(kind: str, text: str) -> str:
        asked[kind] = asked.get(kind, 0) + 1
        if kind == "plain":
            return f"TR {text}"
        print(f"[babeldoc] request {asked['llm']} (tail): {text[-300:]!r}")
        items = _babeldoc_batch(text)
        if items is not None:
            return json.dumps([{"id": item["id"], "output": f"TR {item['input']}"} for item in items],
                              ensure_ascii=False)
        marker = "Now translate the following text:"
        if marker in text:  # 一次一段：提示词最后就是原文，回的是译文本身
            return "TR " + text.rsplit(marker, 1)[1].strip()
        return "[]"  # 术语提取：没有要加的术语

    started = time.monotonic()
    result = babeldoc_component.translate_pdf(source, work / "run", pages=[1, 2], lang_in="en", lang_out="zh",
                                              translate=translate, workers=2,
                                              custom_prompt=babeldoc_component.LITERAL_RULES)
    print(f"[babeldoc] translated in {time.monotonic() - started:.0f}s: ok={result.ok} error={result.error!r} "
          f"requests={result.requests} asked={asked} version={result.version}")
    text = ""
    pages = 0
    if result.ok:
        with fitz.open(result.output_pdf) as out:
            pages = out.page_count
            # BabelDOC 排出来的词与词之间是不换行空格（\xa0），比对前换回普通空格
            text = " ".join(page.get_text() for page in out).replace("\xa0", " ")
        print(f"[babeldoc] output: {pages} pages, text: {text[:300]!r}")
    print(f"[babeldoc] worker output (tail): {result.detail[-1500:]}")
    installer.uninstall()
    if own_pdf:
        pdf_installer.uninstall()
    return result.ok and pages == 2 and "TR " in text and "20120826" in text and installer.installed_dirs() is None


def _babeldoc_batch(prompt: str) -> list[dict] | None:
    """BabelDOC 整批翻译的提示词最后是一个 JSON 数组：[{"id": 0, "input": "...", ...}, ...]。"""
    if '"output"' not in prompt:
        return None
    decoder = json.JSONDecoder()
    found = None
    for start in (index for index, char in enumerate(prompt) if char == "["):
        try:
            value, _end = decoder.raw_decode(prompt, start)
        except ValueError:
            continue
        if isinstance(value, list) and value and all(isinstance(item, dict) and "input" in item for item in value):
            found = value
    return found


def main(argv: list[str]) -> int:
    # Windows 控制台默认 cp1252，打不出中文报错。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if not os.environ.get("TRANSLATOR_APP_DATA_DIR"):
        os.environ["TRANSLATOR_APP_DATA_DIR"] = tempfile.mkdtemp(prefix="component-smoke-")
    print(f"data dir: {os.environ['TRANSLATOR_APP_DATA_DIR']}")
    checks = {"pdf": _pdf, "ocr": _ocr, "ppocr": _ppocr, "babeldoc": _babeldoc}
    names = [name for name in argv if name in checks] or list(checks)
    results = {name: checks[name]() for name in names}
    print("RESULT: " + ", ".join(f"{name}={'OK' if ok else 'FAILED'}" for name, ok in results.items()))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
