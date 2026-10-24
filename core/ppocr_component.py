"""中文、日文线条字识别（PP-OCR）：在单独的子进程里跑。

翻译引擎的安装包里没有 NumPy（见 packaging/sidecar 的 excludes）。识别用的 NumPy、OpenCV
来自「PDF 处理组件」，ONNX Runtime 和模型来自一键安装的识别包
（``core.ppocr_component_install``），它们只进识别子进程：原生库出问题时崩的是子进程，
不会拖垮翻译引擎（和 PDF 转 Word 同一个做法，见 ``core.pdf_component``）。

子进程开着等请求（一页一行 JSON：页面图片、结果写到哪），一份图纸的各页不用每页重新
加载模型；空闲两分钟后关掉，下次用到时再开。请求和回话都只用 ASCII（JSON 转义），
Windows 上管道的编码是什么都不要紧。

本模块在 ``core`` 下，不能从 ``api`` 导入。
"""
from __future__ import annotations

import collections
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from core.pdf_component import _sidecar_command

WORKER_FLAG = "--ppocr-worker"
IDLE_SECONDS = 120.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_STDERR_TAIL = 40

ReaderFn = Callable[..., list[dict]]


class OcrReaderError(RuntimeError):
    """识别子进程没能启动或中途退出；消息是给用户看的一句话。"""


# ---------------------------------------------------------------------------
# 装在哪里
# ---------------------------------------------------------------------------


def _importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def numpy_site_dir() -> Path | None:
    """NumPy、OpenCV 所在的 site-packages：一键安装的「PDF 处理组件」，或用户自己装的
    pdf2docx 环境；源码运行时是当前解释器自己的。都没有时返回 None。"""
    from core import pdf_component

    managed = pdf_component.managed_site_dir()
    if managed is not None:
        return managed
    try:
        state = pdf_component.component_state()
    except Exception as exc:  # noqa: BLE001 - an unreadable component counts as missing
        logger.debug(f"[文字识别组件] 读取 PDF 处理组件状态失败：{exc!r}")
        state = None
    if state is not None and state.available and state.source not in ("", "bundled"):
        site_dir = Path(state.source)
        if (site_dir / "numpy").is_dir() and (site_dir / "cv2").is_dir():
            return site_dir
    if not getattr(sys, "frozen", False) and _importable("numpy") and _importable("cv2"):
        origin = importlib.util.find_spec("numpy").origin
        return Path(origin).parents[1] if origin else None
    return None


def reader_paths() -> tuple[Path, Path, Path] | None:
    """(识别包的 site-packages, 模型目录, NumPy 所在的 site-packages)；缺一样就是 None。"""
    from core.ppocr_component_install import installed_dirs

    try:
        dirs = installed_dirs()
    except Exception as exc:  # noqa: BLE001 - a damaged record counts as not installed
        logger.debug(f"[文字识别组件] 读取一键安装记录失败：{exc!r}")
        return None
    if dirs is None:
        return None
    numpy_site = numpy_site_dir()
    return (dirs[0], dirs[1], numpy_site) if numpy_site is not None else None


# ---------------------------------------------------------------------------
# 子进程
# ---------------------------------------------------------------------------


def _send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _plain(value: Any) -> Any:
    """NumPy 的数字转成 Python 的（行里的框、分数可能是 NumPy 算出来的）。"""
    if hasattr(value, "tolist"):
        return value.tolist()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def worker_main(site_dir: str, models: str, numpy_site: str) -> int:
    """子进程入口（``launch_sidecar.py --ppocr-worker <site-packages> <models> <numpy site>``）。"""
    try:
        # 组件目录放到 sys.path 最后：安装包自带的模块优先，NumPy、OpenCV、ONNX Runtime 只在这里有。
        for path in (numpy_site, site_dir):
            if path not in sys.path:
                sys.path.append(path)
        # 先于 OpenCV 载入：Windows 上让随识别包带的 VC++ 运行库先进进程，不用系统里旧的。
        import onnxruntime
        from PIL import Image

        from core.pdf_vector.cjk_ocr import ppocr_rows
        from core.ppocr_component_install import DET_MODEL, REC_MODEL
        from core.ppocr_engine import PPOCR

        Image.MAX_IMAGE_PIXELS = None
        engine = PPOCR(Path(models) / DET_MODEL, Path(models) / REC_MODEL)
    except BaseException as exc:  # noqa: BLE001 - report every failure to the parent
        _send({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]})
        return 1
    _send({"ok": True, "onnxruntime": str(onnxruntime.__version__)})
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return 0
        try:
            request = json.loads(line.decode("utf-8"))
            with Image.open(request["image"]) as image:
                gray = image.convert("L")
            rows = ppocr_rows(gray, engine, lambda done, total: _send({"progress": [done, total]}))
            Path(request["out"]).write_text(json.dumps(rows, ensure_ascii=False, default=_plain),
                                            encoding="utf-8")
            _send({"done": len(rows)})
        except Exception as exc:  # noqa: BLE001 - one page only costs that page
            _send({"error": f"{type(exc).__name__}: {exc}"[:400]})


class _Worker:
    """父进程这边的一个识别子进程。"""

    def __init__(self, paths: tuple[Path, Path, Path]) -> None:
        self.paths = paths
        self.version = ""
        self._stderr: collections.deque[str] = collections.deque(maxlen=_STDERR_TAIL)
        command = _sidecar_command(WORKER_FLAG, *(str(path) for path in paths))
        try:
            self._proc = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=_NO_WINDOW)
        except (OSError, ValueError) as exc:
            logger.warning(f"[文字识别组件] 识别进程无法启动：{exc!r}")
            raise OcrReaderError("文字识别组件无法运行。") from exc
        threading.Thread(target=self._drain_stderr, name="ppocr-stderr", daemon=True).start()
        hello = self._next()
        if not hello.get("ok"):
            self.close()
            logger.warning(f"[文字识别组件] 识别进程启动失败：{hello.get('error') or self.stderr_tail()}")
            raise OcrReaderError("文字识别组件无法运行。")
        self.version = str(hello.get("onnxruntime") or "")

    def _drain_stderr(self) -> None:
        stream = self._proc.stderr
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            self._stderr.append(raw.decode("utf-8", "replace").rstrip())

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)[-800:]

    def _next(self) -> dict[str, Any]:
        """下一条回话；子进程退出了就是 {}。不是 JSON 的行（原生库打的字）跳过。"""
        stream = self._proc.stdout
        while stream is not None:
            line = stream.readline()
            if not line:
                return {}
            try:
                message = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(message, dict):
                return message
        return {}

    def alive(self) -> bool:
        return self._proc.poll() is None

    def read(self, gray, progress: Callable[[int, int], None] | None) -> list[dict]:
        with tempfile.TemporaryDirectory(prefix="translator-ocr-", ignore_cleanup_errors=True) as folder:
            image, out = Path(folder) / "page.png", Path(folder) / "rows.json"
            gray.save(image, compress_level=1)
            request = json.dumps({"image": str(image), "out": str(out)}) + "\n"
            try:
                assert self._proc.stdin is not None
                self._proc.stdin.write(request.encode("ascii"))
                self._proc.stdin.flush()
            except (OSError, ValueError) as exc:
                self.close()
                raise OcrReaderError("文字识别组件的识别进程意外退出。") from exc
            while True:
                message = self._next()
                if not message:
                    self.close()
                    logger.warning(f"[文字识别组件] 识别进程意外退出：{self.stderr_tail()}")
                    raise OcrReaderError("文字识别组件的识别进程意外退出。")
                if "progress" in message:
                    if progress is not None:
                        done, total = message["progress"]
                        progress(int(done), int(total))
                    continue
                if "error" in message:
                    logger.warning(f"[文字识别组件] 识别这一页出错：{message['error']}")
                    raise OcrReaderError("文字识别组件识别这一页时出错。")
                if "done" in message:
                    rows = json.loads(out.read_text(encoding="utf-8"))
                    return rows if isinstance(rows, list) else []

    def close(self) -> None:
        proc = self._proc
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


class _Reader:
    """全进程共用一个识别子进程，一次识别一页；空闲 IDLE_SECONDS 后关掉。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._worker: _Worker | None = None
        self._timer: threading.Timer | None = None

    def read(self, paths: tuple[Path, Path, Path], gray,
             progress: Callable[[int, int], None] | None = None) -> list[dict]:
        with self._lock:
            self._cancel_timer()
            try:
                worker = self._worker
                if worker is not None and (worker.paths != paths or not worker.alive()):
                    worker.close()
                    worker = self._worker = None
                if worker is None:
                    worker = self._worker = _Worker(paths)
                try:
                    return worker.read(gray, progress)
                except OcrReaderError:
                    if not worker.alive():
                        self._worker = None
                    raise
                except BaseException:
                    # 半路停下（进度回调出错等）：子进程还在回这一页，回话不能留给下一页读。
                    worker.close()
                    self._worker = None
                    raise
            finally:
                if self._worker is not None:
                    self._timer = threading.Timer(IDLE_SECONDS, self._idle)
                    self._timer.daemon = True
                    self._timer.start()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _idle(self) -> None:
        if not self._lock.acquire(blocking=False):
            return  # 正在识别：识别完会重新计时
        try:
            self.close_locked()
        finally:
            self._lock.release()

    def close_locked(self) -> None:
        self._cancel_timer()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.close()

    def close(self) -> None:
        with self._lock:
            self.close_locked()


_READER = _Reader()


def make_reader() -> ReaderFn | None:
    """原位替换用的识别函数 ``reader(gray, progress) -> rows``（见 core.pdf_vector.extract.run）；
    识别包或「PDF 处理组件」没装时返回 None。"""
    paths = reader_paths()
    if paths is None:
        return None

    def read(gray, progress: Callable[[int, int], None] | None = None) -> list[dict]:
        return _READER.read(paths, gray, progress)

    return read


def shutdown_reader() -> None:
    """关掉识别子进程（卸载前、测试结束时）。"""
    _READER.close()


def probe_in_subprocess(site_dir: Path, models: Path, numpy_site: Path) -> tuple[bool, str, str]:
    """(ok, ONNX Runtime 版本, detail)：开一个识别进程认一行字，再关掉。"""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("L", (900, 160), 255)
    ImageDraw.Draw(image).text((20, 40), "Plan de masse 2026", fill=0, font=ImageFont.load_default(size=56))
    try:
        worker = _Worker((site_dir, models, numpy_site))
    except OcrReaderError as exc:
        return False, "", str(exc)
    try:
        rows = worker.read(image, None)
    except OcrReaderError as exc:
        return False, worker.version, f"{exc} {worker.stderr_tail()}".strip()
    finally:
        worker.close()
    text = " ".join(str(row.get("source_text") or "") for row in rows)
    if "masse" not in text.casefold():
        return False, worker.version, f"probe read {text!r}"
    return True, worker.version, ""
