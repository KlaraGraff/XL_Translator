"""在子进程里调用用户安装的格式适配器（扩展性阶段 E）。

随应用提供的适配器在应用进程里加载（``InProcessAdapterHost``）；用户装进来的第三方代码一律
放到子进程里跑：``scan`` / ``extract`` / ``write`` 每次起一个翻译引擎进程
（``translator-sidecar --adapter-host <目录> <动作>``，源码运行时是 ``launch_sidecar.py``），
请求 JSON 走 stdin，结果 JSON 走 stdout，适配器自己打印的内容和报错走 stderr、写进日志。

这样做是为了**崩溃隔离**：适配器卡死会被超时杀掉、抛异常或崩溃只算这个文件失败，翻译引擎
不受影响；子进程的环境变量里没有应用数据目录和任何密钥，工作目录是临时目录。它**不是沙箱**：
子进程仍以用户身份运行，能读写用户的文件。

执行器只认 ``AdapterHost`` 的三个方法，看不出两种宿主的区别。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from loguru import logger

from core.format_adapter import (
    AdapterError,
    AdapterManifest,
    InProcessAdapterHost,
    load_manifest,
    validate_units,
)
from translator_adapter import MODES, OutputFile, ScanItem, TextUnit, TranslatedUnit

HOST_FLAG = "--adapter-host"
ACTIONS = ("scan", "extract", "write")

#: 超时（秒）：扫描一个文件；取字或写回一个文件。到时杀掉子进程，这个文件记为失败。
SCAN_TIMEOUT = 30.0
FILE_TIMEOUT = 120.0

#: 子进程只拿到这些环境变量（不传应用数据目录、不传任何密钥）。
_PASSED_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT", "WINDIR")
#: 结果行的前缀：适配器自己往 stdout 打印的内容已被转到 stderr，这里再加一道保险。
RESULT_PREFIX = "@@translator-adapter-result@@ "
_STDERR_LINES = 40
_STDERR_LINE_CHARS = 400
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _sidecar_command(*args: str) -> list[str]:
    """再起一个翻译引擎进程：冻结的安装包里是它自己，源码运行时是 launch_sidecar.py。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    launcher = Path(__file__).resolve().parents[1] / "scripts" / "launch_sidecar.py"
    return [sys.executable, str(launcher), *args]


def child_environment(temporary_dir: str) -> dict[str, str]:
    env = {name: os.environ[name] for name in _PASSED_ENV if os.environ.get(name)}
    env.update(
        TMPDIR=temporary_dir,
        TEMP=temporary_dir,
        TMP=temporary_dir,
        PYTHONIOENCODING="utf-8",
        PYTHONUTF8="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return env


class SubprocessAdapterHost:
    """一个用户安装的适配器：每次调用起一个子进程，只交换 JSON。"""

    def __init__(
        self,
        directory: str | Path,
        manifest: AdapterManifest | None = None,
        *,
        scan_timeout: float = SCAN_TIMEOUT,
        file_timeout: float = FILE_TIMEOUT,
    ) -> None:
        self.directory = Path(directory)
        self.manifest = manifest or load_manifest(self.directory)
        self.scan_timeout = scan_timeout
        self.file_timeout = file_timeout

    @property
    def log_label(self) -> str:
        return f"[扩展 {self.manifest.label.get('zh') or self.manifest.id}]"

    def _log_stderr(self, text: str) -> None:
        lines = [line.rstrip() for line in (text or "").splitlines() if line.strip()]
        for line in lines[-_STDERR_LINES:]:
            logger.info(f"{self.log_label} {line[:_STDERR_LINE_CHARS]}")

    def _call(self, action: str, request: Mapping[str, Any], timeout: float) -> Any:
        command = _sidecar_command(HOST_FLAG, str(self.directory), action)
        with tempfile.TemporaryDirectory(prefix="translator-adapter-") as workdir:
            try:
                completed = subprocess.run(
                    command,
                    input=json.dumps(request, ensure_ascii=False),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    cwd=workdir,
                    env=child_environment(workdir),
                    check=False,
                    creationflags=_NO_WINDOW,
                )
            except subprocess.TimeoutExpired as exc:
                partial = exc.stderr or ""
                self._log_stderr(partial.decode("utf-8", "replace") if isinstance(partial, bytes) else partial)
                raise AdapterError(f"处理超时（超过 {int(timeout)} 秒），已停止") from exc
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise AdapterError(f"无法启动扩展进程：{exc}") from exc
        self._log_stderr(completed.stderr)
        for line in reversed((completed.stdout or "").splitlines()):
            if not line.startswith(RESULT_PREFIX):
                continue
            try:
                payload = json.loads(line[len(RESULT_PREFIX):])
            except ValueError:
                break
            if isinstance(payload, dict) and payload.get("error"):
                raise AdapterError(str(payload["error"]))
            if isinstance(payload, dict) and "result" in payload:
                return payload["result"]
            break
        tail = (completed.stderr or "").strip().splitlines()[-1:] or [""]
        raise AdapterError(
            f"扩展进程异常退出（代码 {completed.returncode}）" + (f"：{tail[0][:_STDERR_LINE_CHARS]}" if tail[0] else "")
        )

    def scan(self, path: Path) -> ScanItem:
        result = self._call("scan", {"path": str(path)}, self.scan_timeout)
        try:
            return ScanItem.from_dict(result)
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"scan 返回值不合约定：{exc}") from exc

    def extract(self, path: Path) -> list[TextUnit]:
        result = self._call("extract", {"path": str(path)}, self.file_timeout)
        if not isinstance(result, list):
            raise AdapterError(f"extract 应返回 TextUnit 列表，实际是 {type(result).__name__}")
        try:
            units = [TextUnit.from_dict(item) for item in result]
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"extract 返回值不合约定：{exc}") from exc
        return validate_units(units)

    def write(
        self,
        path: Path,
        units: list[TranslatedUnit],
        mode: str,
        out_path: Path,
        options: Mapping[str, Any],
    ) -> OutputFile:
        if mode not in MODES:
            raise ValueError(f"unknown write mode: {mode!r}")
        request = {
            "path": str(path),
            "units": [unit.to_dict() for unit in units],
            "mode": mode,
            "out_path": str(out_path),
            "options": dict(options),
        }
        result = self._call("write", request, self.file_timeout)
        try:
            return OutputFile.from_dict(result)
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"write 返回值不合约定：{exc}") from exc


# ── 子进程这一侧 ──────────────────────────────────────────────────────────────


def _run_action(directory: Path, action: str, request: Mapping[str, Any]) -> Any:
    # 适配器同目录的其他 .py 可以直接 import；安装检查已扫描过它们。
    sys.path.insert(0, str(directory))
    host = InProcessAdapterHost(directory)
    path = Path(str(request.get("path") or ""))
    if action == "scan":
        return host.scan(path).to_dict()
    if action == "extract":
        return [unit.to_dict() for unit in host.extract(path)]
    units = [TranslatedUnit.from_dict(item) for item in request.get("units") or []]
    return host.write(
        path,
        units,
        str(request.get("mode") or ""),
        Path(str(request.get("out_path") or "")),
        dict(request.get("options") or {}),
    ).to_dict()


def host_main(directory: str, action: str) -> int:
    """``--adapter-host <目录> <动作>``：读 stdin 的请求，跑一次适配器，结果写一行到 stdout。"""
    sys.dont_write_bytecode = True
    # 引擎按 UTF-8 收发。冻结版不认 PYTHONUTF8 / PYTHONIOENCODING，Windows 管道默认按
    # 系统代码页（cp1252、cp936…），中文路径读错、结果写不出，这里显式改成 UTF-8。
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict" if stream is sys.stdin else "replace")
    result_stream = sys.stdout
    # 适配器里的 print() 不能混进结果：执行期间 stdout 转到 stderr（写进日志）。
    sys.stdout = sys.stderr
    try:
        if action not in ACTIONS:
            raise AdapterError(f"未知的动作：{action}")
        request = json.loads(sys.stdin.read() or "{}")
        if not isinstance(request, dict):
            raise AdapterError("请求格式不正确")
        payload: dict[str, Any] = {"result": _run_action(Path(directory), action, request)}
        code = 0
    except AdapterError as exc:
        payload, code = {"error": str(exc)}, 1
    except Exception as exc:  # noqa: BLE001 - every failure goes back to the engine as a message
        payload, code = {"error": f"{exc.__class__.__name__}: {exc}" if str(exc) else exc.__class__.__name__}, 1
    finally:
        sys.stdout = result_stream
    result_stream.write(RESULT_PREFIX + json.dumps(payload, ensure_ascii=False) + "\n")
    result_stream.flush()
    return code


__all__ = [
    "ACTIONS",
    "FILE_TIMEOUT",
    "HOST_FLAG",
    "SCAN_TIMEOUT",
    "SubprocessAdapterHost",
    "child_environment",
    "host_main",
]
