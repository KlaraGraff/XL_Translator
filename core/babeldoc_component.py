"""「段落重排组件（BabelDOC）」：在单独的子进程里跑 BabelDOC，翻译交回给翻译引擎。

子进程（``launch_sidecar.py --babeldoc-worker <任务文件>``）里只有 BabelDOC 和它的依赖：
组件目录接在 ``sys.path`` 最后，安装包自带的库优先；NumPy、OpenCV、PyMuPDF 来自「PDF 处理
组件」。原生库出问题时崩的是子进程，不会拖垮翻译引擎（和 PDF 转 Word、文字识别同一个做法）。

BabelDOC 自己不连模型：它的翻译器换成 ``AppTranslator``，每次要译文时把它拼好的提示词写到
stdout，翻译引擎这边（``translate_pdf``）用用户设置的文本翻译模型（同一套连接、并发、重试、
超时）请求，再把译文写回子进程的 stdin。来回都是一行 JSON，只用 ASCII（JSON 转义），Windows
上管道的编码是什么都不要紧。BabelDOC 和第三方库打印的东西都改到 stderr，不会混进 stdout。

本模块在 ``core`` 下，不能从 ``api`` 导入。
"""
from __future__ import annotations

import collections
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from core.pdf_component import _sidecar_command

WORKER_FLAG = "--babeldoc-worker"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_STDERR_TAIL = 60
_PROBE_TIMEOUT = 600.0
# 子进程这么久没有任何动静（没进度、没要译文、手上也没有在等的译文）就当它卡住了。
# BabelDOC 排版一页几秒，版面识别整份文件也就一两分钟，二十分钟一声不吭不正常。
_IDLE_LIMIT = 20 * 60.0

#: 翻译回调：(kind, text) -> 译文。kind 是 "llm"（BabelDOC 拼好的整段提示词，原样发给模型）
#: 或 "plain"（一段要翻的文字，按 BabelDOC 的逐段提示词发）。失败时抛异常。
TranslateFn = Callable[[str, str], str]

# 模型最容易「顺手改」的字面值：ICAO 9303 实测页脚 14/6/24 被改成「2024年6月14日」，NLD 译成
# 「荷兰」，示例姓名被译成中文（2026-11-27 Mac 实测）。BabelDOC 提示词是英文的，这条也用英文。
LITERAL_RULES = (
    "Copy every number, date, time, code, identifier, unit, version, reference mark, country or language code, "
    "e-mail address, URL and quoted sample value exactly as written in the source, character for character. "
    "Never reformat a date (14/6/24 stays 14/6/24, 20120826 stays 20120826), never translate a code "
    "(NLD stays NLD), and never translate names or values inside sample data, machine-readable lines or "
    "tables of example values. Keep product and brand names as written."
)


class BabelDocError(RuntimeError):
    """子进程没能启动或中途退出；消息是给用户看的一句话。"""


# ---------------------------------------------------------------------------
# 装在哪里
# ---------------------------------------------------------------------------


def numpy_site_dir() -> Path | None:
    """NumPy、OpenCV、PyMuPDF 所在的 site-packages（「PDF 处理组件」），与文字识别组件同一份。"""
    from core.ppocr_component import numpy_site_dir as find

    return find()


def component_paths() -> tuple[Path, Path, Path] | None:
    """(BabelDOC 的 site-packages, 它的 HOME, NumPy 所在的 site-packages)；缺一样就是 None。"""
    from core.babeldoc_component_install import installed_dirs

    try:
        dirs = installed_dirs()
    except Exception as exc:  # noqa: BLE001 - a damaged record counts as not installed
        logger.debug(f"[段落重排组件] 读取一键安装记录失败：{exc!r}")
        return None
    if dirs is None:
        return None
    numpy_site = numpy_site_dir()
    return (dirs[0], dirs[1], numpy_site) if numpy_site is not None else None


def babeldoc_lang(code: str) -> str:
    """我们的语言代码 → BabelDOC 的。BabelDOC 按代码里的 CN / TW / HK / JP(JA) / KR 选字体，
    只写 zh、ko 会落到西文字体那一组。"""
    value = str(code or "").strip()
    lower = value.lower().replace("_", "-")
    if lower in ("", "auto"):
        return "en"
    if lower in ("zh", "zh-cn", "zh-hans", "zh-sg"):
        return "zh-CN"
    if lower in ("zh-tw", "zh-hant"):
        return "zh-TW"
    if lower in ("zh-hk", "zh-mo"):
        return "zh-HK"
    if lower.startswith("ko"):
        return "ko-KR"
    if lower.startswith("ja"):
        return "ja"
    return value


def page_ranges(pages: list[int]) -> str:
    """[1, 2, 3, 7] → "1-3,7"（BabelDOC 的 --pages 写法，从 1 数）。"""
    parts: list[str] = []
    numbers = sorted({int(page) for page in pages})
    index = 0
    while index < len(numbers):
        start = end = numbers[index]
        while index + 1 < len(numbers) and numbers[index + 1] == end + 1:
            index += 1
            end = numbers[index]
        parts.append(str(start) if start == end else f"{start}-{end}")
        index += 1
    return ",".join(parts)


# ---------------------------------------------------------------------------
# 子进程
# ---------------------------------------------------------------------------


class _Protocol:
    """子进程这边的 stdout：一行一条 JSON。fd 1 改指 stderr，库里的 print 进不来。"""

    def __init__(self) -> None:
        self._out = os.fdopen(os.dup(1), "w", encoding="ascii", errors="backslashreplace", buffering=1)
        os.dup2(2, 1)
        sys.stdout = sys.stderr
        self._lock = threading.Lock()

    def send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, ensure_ascii=True)
        with self._lock:
            self._out.write(line + "\n")
            self._out.flush()


class _Calls:
    """子进程这边：发出「要译文」，等翻译引擎回话。BabelDOC 会同时从几个线程来要。"""

    def __init__(self, protocol: _Protocol) -> None:
        self._protocol = protocol
        self._lock = threading.Lock()
        self._next = 0
        self._pending: dict[int, list[Any]] = {}
        self._closed = False
        threading.Thread(target=self._read, name="babeldoc-replies", daemon=True).start()

    def call(self, kind: str, text: str) -> str:
        event = threading.Event()
        with self._lock:
            if self._closed:
                raise RuntimeError("translator connection closed")
            call_id = self._next
            self._next += 1
            self._pending[call_id] = [event, None]
        self._protocol.send({"call": {"id": call_id, "kind": kind, "text": text}})
        event.wait()
        with self._lock:
            reply = self._pending.pop(call_id)[1] or {}
        if "text" not in reply:
            raise RuntimeError(str(reply.get("error") or "translation failed"))
        return str(reply["text"])

    def _read(self) -> None:
        for raw in iter(sys.stdin.buffer.readline, b""):
            try:
                reply = json.loads(raw.decode("utf-8"))
                call_id = int(reply["id"])
            except (ValueError, KeyError, TypeError) as exc:
                logger.debug(f"[段落重排组件] 读不懂的回话，已忽略：{exc!r}")
                continue
            with self._lock:
                slot = self._pending.get(call_id)
                if slot is not None:
                    slot[1] = reply
                    slot[0].set()
        # 翻译引擎那边关了管道（任务停止、引擎退出）：等着的全部失败，子进程不再留着。
        with self._lock:
            self._closed = True
            for slot in self._pending.values():
                slot[1] = {"error": "translator connection closed"}
                slot[0].set()
        os._exit(3)


def _placeholder_methods(cls):
    """占位符照 BabelDOC 的 OpenAI 翻译器：它的提示词就是按这种写法讲的。"""

    def formula(self, placeholder_id):
        return "{v" + str(placeholder_id) + "}", f"{{\\s*v\\s*{placeholder_id}\\s*}}"

    def left(self, placeholder_id):
        return (f"<style id='{placeholder_id}'>",
                f"<\\s*style\\s*id\\s*=\\s*'\\s*{placeholder_id}\\s*'\\s*>")

    def right(self, placeholder_id):
        return "</style>", r"<\s*\/\s*style\s*>"

    cls.get_formular_placeholder = formula
    cls.get_rich_text_left_placeholder = left
    cls.get_rich_text_right_placeholder = right
    return cls


def _make_translators():
    from babeldoc.translator.translator import BaseTranslator  # pyright: ignore[reportMissingImports]  # only in the component

    @_placeholder_methods
    class AppTranslator(BaseTranslator):
        # BabelDOC 的缓存表里引擎名最长 20 个字符；缓存不用（ignore_cache），每次都问模型。
        name = "translator-app"

        def __init__(self, lang_in: str, lang_out: str, call: Callable[[str, str], str]) -> None:
            super().__init__(lang_in, lang_out, True)
            self.model = "app"
            self._call = call

        def do_llm_translate(self, text, rate_limit_params=None):
            if text is None:
                return None  # BabelDOC 用它探测「能不能整批翻」
            return self._call("llm", str(text))

        def do_translate(self, text, rate_limit_params=None):
            return self._call("plain", str(text))

    @_placeholder_methods
    class EchoTranslator(BaseTranslator):
        """试排用：原文照抄，不连模型。不支持整批翻，BabelDOC 改走逐段翻。"""

        name = "translator-probe"

        def __init__(self, lang_in: str, lang_out: str) -> None:
            super().__init__(lang_in, lang_out, True)
            self.model = "echo"

        def do_llm_translate(self, text, rate_limit_params=None):
            raise NotImplementedError

        def do_translate(self, text, rate_limit_params=None):
            return str(text)

    return AppTranslator, EchoTranslator


def _probe_pdf(path: Path) -> None:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    text = ("Hello world. This short paragraph checks that the layout model, the fonts and the PDF writer "
            "all load on this computer. The value 42 must stay as it is.")
    page.insert_textbox(pymupdf.Rect(72, 90, 523, 300), text, fontsize=12, fontname="helv")
    doc.save(str(path))
    doc.close()


def _run_babeldoc(protocol: _Protocol, job: dict[str, Any]) -> dict[str, Any]:
    import asyncio
    import logging

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    from babeldoc import __version__ as version  # pyright: ignore[reportMissingImports]  # only in the component
    from babeldoc.format.pdf import high_level  # pyright: ignore[reportMissingImports]  # only in the component
    from babeldoc.format.pdf.translation_config import TranslationConfig, WatermarkOutputMode  # pyright: ignore[reportMissingImports]  # only in the component
    from babeldoc.glossary import Glossary, GlossaryEntry  # pyright: ignore[reportMissingImports]  # only in the component

    high_level.init()
    app_translator, echo_translator = _make_translators()
    probe = job.get("mode") == "probe"
    work = Path(job["working_dir"])
    work.mkdir(parents=True, exist_ok=True)
    source = Path(job["input"]) if not probe else work / "probe.pdf"
    if probe:
        _probe_pdf(source)
    lang_in, lang_out = str(job.get("lang_in") or "en"), str(job.get("lang_out") or "zh-CN")
    if probe:
        translator = echo_translator(lang_in, lang_out)
    else:
        calls = _Calls(protocol)
        translator = app_translator(lang_in, lang_out, calls.call)
    protocol.send({"ready": True, "version": str(version)})
    workers = max(1, int(job.get("workers") or 1))
    glossary_entries = [GlossaryEntry(str(source_text), str(target_text))
                        for source_text, target_text in job.get("glossary") or [] if source_text and target_text]
    config = TranslationConfig(
        translator=translator,
        input_file=str(source),
        lang_in=lang_in,
        lang_out=lang_out,
        doc_layout_model=None,
        pages=job.get("pages") or None,
        output_dir=str(job.get("output_dir") or work / "out"),
        working_dir=str(work / "babeldoc"),
        no_dual=True,
        qps=workers,
        pool_max_workers=workers,
        term_pool_max_workers=workers,
        use_rich_pbar=False,
        report_interval=1.0,
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
        # 哪些页是扫描页由我们自己判断过了，送来的都是有文字层的页。
        skip_scanned_detection=True,
        custom_system_prompt=str(job.get("custom_prompt") or "") or None,
        glossaries=[Glossary("translator-terms", glossary_entries)] if glossary_entries else None,
        auto_extract_glossary=not probe,
    )

    async def run() -> Any:
        result = None
        last = -1.0
        async for event in high_level.async_translate(config):
            kind = event.get("type")
            if kind in ("progress_update", "progress_end"):
                overall = float(event.get("overall_progress") or 0.0)
                if overall - last >= 0.5 or kind == "progress_end":
                    last = overall
                    protocol.send({"progress": round(overall, 2), "stage": str(event.get("stage") or "")})
            elif kind == "finish":
                result = event.get("translate_result")
            elif kind == "error":
                raise RuntimeError(str(event.get("error") or "BabelDOC failed"))
        return result

    result = asyncio.run(run())
    mono = getattr(result, "mono_pdf_path", None) or getattr(result, "no_watermark_mono_pdf_path", None)
    if not mono or not Path(str(mono)).is_file():
        raise RuntimeError("BabelDOC did not write a translated PDF")
    if probe:
        import pymupdf

        with pymupdf.open(str(mono)) as doc:
            text = doc[0].get_text() if doc.page_count == 1 else ""
        if "42" not in text or "Hello" not in text:
            raise RuntimeError(f"probe output unexpected: {text[:200]!r}")
    return {"mono": str(mono), "version": str(version)}


def worker_main(job_path: str) -> int:
    """子进程入口（``launch_sidecar.py --babeldoc-worker <任务文件>``）。"""
    protocol = _Protocol()
    try:
        job = json.loads(Path(job_path).read_text(encoding="utf-8"))
        # 组件目录放到 sys.path 最后：安装包自带的模块优先，NumPy、PyMuPDF、BabelDOC 只在这里有。
        for path in job.get("site_dirs") or []:
            if path and path not in sys.path:
                sys.path.append(path)
        # BabelDOC 的缓存目录是 Path.home()/.cache/babeldoc（字体、版面模型在那里）：指到组件目录里。
        home = str(job["home"])
        os.environ["HOME"] = home
        os.environ["USERPROFILE"] = home
        # 先于 PyMuPDF、OpenCV 载入：Windows 上让随组件带的 VC++ 运行库先进进程，不用 PDF 处理组件
        # 里那份旧的 msvcp140.dll（同名的库进程里只有一份，先进来的算数）。
        import onnxruntime  # noqa: F401  # pyright: ignore[reportMissingImports]  # only in the component

        done = _run_babeldoc(protocol, job)
    except BaseException as exc:  # noqa: BLE001 - report every failure to the parent
        import traceback

        traceback.print_exc(file=sys.stderr)
        protocol.send({"error": f"{type(exc).__name__}: {exc}"[:600]})
        return 1
    protocol.send({"done": done})
    return 0


# ---------------------------------------------------------------------------
# 翻译引擎这边
# ---------------------------------------------------------------------------


@dataclass
class BabelDocResult:
    ok: bool
    output_pdf: str = ""
    stopped: bool = False
    #: 给用户看的一句话（失败时）
    error: str = ""
    #: 写进诊断日志的细节（子进程的报错、stderr 结尾）
    detail: str = ""
    requests: int = 0
    failed_requests: int = 0
    version: str = ""


class _Worker:
    def __init__(self, job: dict[str, Any], job_path: Path) -> None:
        job_path.parent.mkdir(parents=True, exist_ok=True)
        job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        self._stderr: collections.deque[str] = collections.deque(maxlen=_STDERR_TAIL)
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        try:
            self.proc = subprocess.Popen(
                _sidecar_command(WORKER_FLAG, str(job_path)),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=_NO_WINDOW,
            )
        except (OSError, ValueError) as exc:
            logger.warning(f"[段落重排组件] 子进程无法启动：{exc!r}")
            raise BabelDocError("段落重排组件无法运行。") from exc
        self._write_lock = threading.Lock()
        threading.Thread(target=self._drain_stderr, name="babeldoc-stderr", daemon=True).start()
        threading.Thread(target=self._read_stdout, name="babeldoc-stdout", daemon=True).start()

    def _drain_stderr(self) -> None:
        stream = self.proc.stderr
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            self._stderr.append(raw.decode("utf-8", "replace").rstrip())

    def _read_stdout(self) -> None:
        stream = self.proc.stdout
        if stream is not None:
            for raw in iter(stream.readline, b""):
                try:
                    message = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue  # 原生库直接写 fd 的字（极少）跳过
                if isinstance(message, dict):
                    self.messages.put(message)
        self.messages.put(None)

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)[-2000:]

    def reply(self, message: dict[str, Any]) -> None:
        data = (json.dumps(message, ensure_ascii=True) + "\n").encode("ascii")
        with self._write_lock:
            try:
                assert self.proc.stdin is not None
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                logger.debug(f"[段落重排组件] 子进程已退出，回话没送到：{exc!r}")

    def close(self) -> None:
        proc = self.proc
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError as exc:
            logger.debug(f"[段落重排组件] 关闭子进程输入失败：{exc!r}")
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.warning("[段落重排组件] 子进程没有按时退出")

    def kill(self) -> None:
        try:
            self.proc.kill()
        except OSError as exc:
            logger.debug(f"[段落重排组件] 结束子进程失败：{exc!r}")
        self.close()


def translate_pdf(
    source: Path,
    work_dir: Path,
    *,
    pages: list[int],
    lang_in: str,
    lang_out: str,
    translate: TranslateFn,
    workers: int = 4,
    custom_prompt: str = "",
    glossary: list[tuple[str, str]] | None = None,
    progress: Callable[[float], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    paths: tuple[Path, Path, Path] | None = None,
) -> BabelDocResult:
    """用 BabelDOC 翻译 ``source`` 的 ``pages``（从 1 数）。输出的 PDF 与原文页数相同，没选的页
    原样留着。``progress`` 收 0–100。"""
    paths = paths or component_paths()
    if paths is None:
        return BabelDocResult(False, error="未安装「段落重排组件」")
    site_dir, home, numpy_site = paths
    work_dir.mkdir(parents=True, exist_ok=True)
    job = {
        "mode": "translate",
        "site_dirs": [str(numpy_site), str(site_dir)],
        "home": str(home),
        "input": str(source),
        "output_dir": str(work_dir / "out"),
        "working_dir": str(work_dir),
        "pages": page_ranges(pages),
        "lang_in": babeldoc_lang(lang_in),
        "lang_out": babeldoc_lang(lang_out),
        "workers": max(1, int(workers)),
        "custom_prompt": custom_prompt,
        "glossary": [list(pair) for pair in glossary or []],
    }
    try:
        worker = _Worker(job, work_dir / "job.json")
    except BabelDocError as exc:
        return BabelDocResult(False, error=str(exc))
    counts = {"requests": 0, "failed": 0, "in_flight": 0}
    lock = threading.Lock()
    pool = ThreadPoolExecutor(max_workers=max(1, int(workers)), thread_name_prefix="babeldoc-call")
    result = BabelDocResult(False)

    def answer(call: dict[str, Any]) -> None:
        call_id = call.get("id")
        try:
            text = translate(str(call.get("kind") or "llm"), str(call.get("text") or ""))
            worker.reply({"id": call_id, "text": text})
        except Exception as exc:  # noqa: BLE001 - BabelDOC decides what an untranslated paragraph becomes
            logger.debug(f"[段落重排组件] 一次翻译请求失败：{exc!r}")
            with lock:
                counts["failed"] += 1
            worker.reply({"id": call_id, "error": str(exc)[:300] or type(exc).__name__})
        finally:
            with lock:
                counts["in_flight"] -= 1

    last_activity = time.monotonic()
    try:
        while True:
            if should_stop is not None and should_stop():
                result.stopped = True
                result.error = "任务已停止"
                break
            try:
                message = worker.messages.get(timeout=0.5)
            except queue.Empty:
                with lock:
                    busy = counts["in_flight"] > 0
                if busy:
                    last_activity = time.monotonic()
                elif time.monotonic() - last_activity > _IDLE_LIMIT:
                    result.error = "段落重排组件长时间没有响应"
                    result.detail = worker.stderr_tail()
                    break
                continue
            last_activity = time.monotonic()
            if message is None:
                result.error = "段落重排组件意外退出"
                result.detail = worker.stderr_tail()
                break
            if "call" in message:
                with lock:
                    counts["requests"] += 1
                    counts["in_flight"] += 1
                pool.submit(answer, message["call"])
            elif "progress" in message:
                if progress is not None:
                    progress(float(message.get("progress") or 0.0))
            elif "ready" in message:
                result.version = str(message.get("version") or "")
            elif "error" in message:
                result.error = "段落重排组件处理这份文件时出错"
                result.detail = f"{message['error']}\n{worker.stderr_tail()}".strip()
                break
            elif "done" in message:
                done = message.get("done") or {}
                output = Path(str(done.get("mono") or ""))
                if output.is_file():
                    result.ok = True
                    result.output_pdf = str(output)
                else:
                    result.error = "段落重排组件没有写出译文 PDF"
                break
    finally:
        if result.ok:
            worker.close()
        else:
            worker.kill()
        pool.shutdown(wait=False, cancel_futures=True)
    result.requests = counts["requests"]
    result.failed_requests = counts["failed"]
    if not result.ok and result.detail:
        logger.warning(f"[段落重排组件] {result.error}：{result.detail[-1500:]}")
    return result


def probe_in_subprocess(site_dir: Path, home: Path, numpy_site: Path) -> tuple[bool, str, str]:
    """(ok, BabelDOC 版本, detail)：在子进程里真排一页英文 PDF（原文照抄，不连模型）。"""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="translator-babeldoc-probe-", ignore_cleanup_errors=True) as folder:
        work = Path(folder)
        job = {"mode": "probe", "site_dirs": [str(numpy_site), str(site_dir)], "home": str(home),
               "working_dir": str(work), "output_dir": str(work / "out"), "lang_in": "en", "lang_out": "zh-CN",
               "workers": 1}
        try:
            worker = _Worker(job, work / "job.json")
        except BabelDocError as exc:
            return False, "", str(exc)
        version = ""
        deadline = time.monotonic() + _PROBE_TIMEOUT
        try:
            while time.monotonic() < deadline:
                try:
                    message = worker.messages.get(timeout=1.0)
                except queue.Empty:
                    continue
                if message is None:
                    return False, version, f"probe exited: {worker.stderr_tail()}"
                if "ready" in message:
                    version = str(message.get("version") or "")
                elif "error" in message:
                    return False, version, f"{message['error']}\n{worker.stderr_tail()}"
                elif "done" in message:
                    return True, str((message.get("done") or {}).get("version") or version), ""
            return False, version, f"probe timed out: {worker.stderr_tail()}"
        finally:
            worker.kill()
