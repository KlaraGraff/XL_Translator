"""关闭软件时会发生什么：在真实 Windows（GitHub Actions windows 机器）上逐个场景量出来。

不动用户数据：每个场景一个临时数据目录、一个本机假模型。翻译引擎按桌面外壳的方式启动
（``python -m api.launcher``，带 ``TRANSLATOR_SIDECAR_PARENT_PID``，Windows 上 ``CREATE_NO_WINDOW``），
然后模拟外壳的几种关闭方式：

- normal：正常关闭——外壳发 ``POST /api/system/shutdown``，最多等 25 秒，再强杀（和 main.rs 一样）。
- shell_gone：外壳被任务管理器结束或崩溃——父进程直接没了，引擎靠自己的看门狗退出。
- killed：引擎被强杀（外壳等满 25 秒后的兜底；Windows 上和 main.rs 一样先 ``taskkill /T``）。

每个场景都在任务跑到一半时关，并且任务里正在跑一个子程序（用一个「取字很慢」的扩展模拟
LibreOffice、ODA、文字识别这类外部程序），然后看：

1. 引擎多久退出；
2. 引擎退出后，它起的子程序还在不在；
3. 临时文件夹里有没有剩下的；
4. 马上重新打开能不能用、上次的任务显示成什么状态。

用法：``python scripts/windows_exit_probe.py [--scenario normal,shell_gone,killed] [--report out.json] [--strict]``

``--strict``：任一场景里子程序留了下来、重新打开失败，或（引擎能自己收尾的 normal / shell_gone）
临时文件夹有残留，就以非零退出，供 PR 检查用。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
# main.rs 的 SIDECAR_STOP_TIMEOUT：外壳最多等这么久就强杀。
SHELL_STOP_BUDGET = 25.0
# 子程序「取字」要睡这么久：远超外壳的等待，才看得出它会不会被留下。
CHILD_SLEEP_SECONDS = 150
MARKER_EXT = "slowfmt"


# ── 假模型 ────────────────────────────────────────────────────────────────


class _FakeModel(BaseHTTPRequestHandler):
    delay = 0.0
    waiting = 0

    def log_message(self, *_args) -> None:
        return

    def _send(self, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._send({"object": "list", "data": [{"id": "fake-model", "object": "model"}]})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        user = "\n".join(
            m["content"] if isinstance(m.get("content"), str) else json.dumps(m.get("content"), ensure_ascii=False)
            for m in body.get("messages") or [] if m.get("role") == "user"
        )
        if "OK" in user and len(user) < 200:
            content = "OK"
        else:
            type(self).waiting += 1
            time.sleep(self.delay)
            try:
                payload = json.loads(user)
            except ValueError:
                payload = None
            out = []
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict) and "id" in item:
                        out.append({"id": item["id"], "action": "translate", "translation": "Traduction", "source_lang": "zh"})
                    else:
                        out.append({"translation": "Traduction", "source_lang": "zh"})
            content = json.dumps(out, ensure_ascii=False)
        self._send({
            "id": "fake", "object": "chat.completion", "model": "fake-model",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })


def start_fake_model(delay: float, handler_out: list | None = None) -> str:
    handler = type("SlowModel", (_FakeModel,), {"delay": delay, "waiting": 0})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if handler_out is not None:
        handler_out.append(handler)
    return f"http://127.0.0.1:{server.server_address[1]}/v1"


def model_wait_scenario(workdir: Path) -> dict:
    """任务正在等模型回复（模型 90 秒才回）时正常关闭：不该等满 12 秒的任务时限。"""
    import openpyxl

    result: dict = {"scenario": "model_wait"}
    app_data = workdir / "model_wait" / "appdata"
    samples = workdir / "model_wait" / "samples"
    app_data.mkdir(parents=True)
    samples.mkdir(parents=True)
    handlers: list = []
    model_url = start_fake_model(delay=90.0, handler_out=handlers)
    shell = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"], creationflags=NO_WINDOW)
    engine = Engine(app_data, shell.pid)
    try:
        configure_model(engine, model_url)
        book = openpyxl.Workbook()
        for row in range(1, 41):
            book.active.cell(row=row, column=1, value=f"设备名称第{row}行检修记录")
        sample = samples / "sample.xlsx"
        book.save(sample)
        start = run_cli(app_data, "translate", str(sample), "--to", "fr", "--yes", "--json", timeout=60)
        result["translate_start"] = start.returncode
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and handlers[0].waiting == 0:
            time.sleep(0.2)
        result["model_requests_waiting"] = handlers[0].waiting
        if handlers[0].waiting == 0:
            result["error"] = f"任务没发出模型请求：{start.stdout[-400:]} {start.stderr[-400:]}"
            return result
        time.sleep(1)
        engine.api("POST", "/api/system/shutdown", timeout=3)
        took = engine.wait_exit(SHELL_STOP_BUDGET)
        result["engine_exit"] = "超时" if took is None else f"{took:.1f} 秒后自己退出"
        result["seconds_to_engine_gone"] = SHELL_STOP_BUDGET if took is None else round(took, 1)
        again = Engine(app_data, shell.pid)
        result["reopen"] = "可用" if again.api("GET", "/api/tasks") is not None else "失败"
        again.api("POST", "/api/system/shutdown")
        again.wait_exit(30)
    finally:
        if engine.process.poll() is None:
            engine.process.kill()
        shell.kill()
    return result


# ── 引擎 ──────────────────────────────────────────────────────────────────


class Engine:
    def __init__(self, app_data: Path, parent_pid: int) -> None:
        env = dict(os.environ)
        env.update(
            TRANSLATOR_APP_DATA_DIR=str(app_data),
            TRANSLATOR_SIDECAR_PARENT_PID=str(parent_pid),
            PYTHONUNBUFFERED="1",
            NO_PROXY="127.0.0.1,localhost",
            no_proxy="127.0.0.1,localhost",
        )
        self.process = subprocess.Popen(
            [sys.executable, "-m", "api.launcher"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, creationflags=NO_WINDOW,
        )
        line = self.process.stdout.readline()
        fields = dict(part.split("=", 1) for part in line.split())
        self.port = int(fields["PORT"])
        self.token = fields["TOKEN"]
        self.started = time.monotonic()

    def api(self, method: str, path: str, body: dict | None = None, timeout: float = 10.0):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"X-Translator-Token": self.token, "Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def descendants(self) -> list[psutil.Process]:
        try:
            return psutil.Process(self.process.pid).children(recursive=True)
        except psutil.Error:
            return []

    def wait_exit(self, timeout: float) -> float | None:
        start = time.monotonic()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        return time.monotonic() - start


def configure_model(engine: Engine, model_url: str) -> None:
    engine.api("PUT", "/api/models/roles/translation", {
        "mode": "cloud", "provider": "custom_openai", "model": "fake-model", "base_url": model_url,
    })
    engine.api("PUT", "/api/keys/custom_openai", {"api_key": "fake-key", "base_url": model_url})


# ── 样例：一个「取字很慢」的扩展，模拟 LibreOffice / ODA / 文字识别这类外部程序 ──────────


def make_slow_extension(folder: Path) -> Path:
    template = ROOT / "cli" / "adapter_template"
    target = folder / "slow-format"
    shutil.copytree(template, target, ignore=shutil.ignore_patterns(".github", "samples", "scripts"))
    manifest = json.loads((target / "adapter.json").read_text(encoding="utf-8"))
    manifest.update(id="slow-format", extensions=[MARKER_EXT])
    manifest["label"] = {"zh": "慢格式", "en": "Slow format", "fr": "Format lent"}
    (target / "adapter.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    code = (target / "adapter.py").read_text(encoding="utf-8")
    code = code.replace(
        'def extract(path: str) -> list[TextUnit]:\n',
        'def extract(path: str) -> list[TextUnit]:\n'
        f'    import time\n    time.sleep({CHILD_SLEEP_SECONDS})\n',
        1,
    )
    (target / "adapter.py").write_text(code, encoding="utf-8")
    return target


def run_cli(app_data: Path, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    env = dict(os.environ, TRANSLATOR_APP_DATA_DIR=str(app_data), NO_PROXY="127.0.0.1,localhost",
               no_proxy="127.0.0.1,localhost", PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-m", "cli", *args], cwd=ROOT, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW)


# ── 临时文件 ───────────────────────────────────────────────────────────────


def temp_entries() -> set[str]:
    base = Path(tempfile.gettempdir())
    try:
        return {entry.name for entry in base.iterdir()}
    except OSError:
        return set()


# ── 场景 ───────────────────────────────────────────────────────────────────


def scenario(name: str, workdir: Path) -> dict:
    result: dict = {"scenario": name}
    app_data = workdir / name / "appdata"
    samples = workdir / name / "samples"
    app_data.mkdir(parents=True)
    samples.mkdir(parents=True)
    model_url = start_fake_model(delay=1.0)

    # 「外壳」：一个只负责活着的进程，它的 pid 交给引擎当父进程。
    shell = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"], creationflags=NO_WINDOW)
    engine = Engine(app_data, shell.pid)
    configure_model(engine, model_url)

    extension = make_slow_extension(workdir / name)
    install = run_cli(app_data, "adapter", "install", str(extension), "--yes", "--json")
    result["extension_install"] = install.returncode
    if install.returncode != 0:
        result["error"] = f"扩展安装失败：{install.stdout[-400:]} {install.stderr[-400:]}"
        engine.process.kill()
        shell.kill()
        return result
    # 装了扩展要重启引擎才认得新类型；顺便验证「关了马上再开」。
    engine.api("POST", "/api/system/shutdown")
    engine.wait_exit(30)
    engine = Engine(app_data, shell.pid)

    sample = samples / f"sample.{MARKER_EXT}"
    sample.write_text("title = 你好\nbody = 世界\n", encoding="utf-8")
    before_temp = temp_entries()
    start = run_cli(app_data, "translate", str(sample), "--to", "fr", "--yes", "--json", timeout=60)
    result["translate_start"] = start.returncode
    try:
        task_id = json.loads(start.stdout).get("task_id")
    except ValueError:
        task_id = None
    if not task_id:
        result["error"] = f"任务没开起来：{start.stdout[-600:]} {start.stderr[-600:]}"
        engine.process.kill()
        shell.kill()
        return result

    # 等子程序真的起来。
    deadline = time.monotonic() + 40
    children: list[psutil.Process] = []
    while time.monotonic() < deadline:
        children = [p for p in engine.descendants() if p.is_running()]
        if children:
            break
        time.sleep(0.5)
    child_pids = [p.pid for p in children]
    result["child_pids_before_close"] = child_pids
    time.sleep(2)

    closed_at = time.monotonic()
    if name == "normal":
        try:
            engine.api("POST", "/api/system/shutdown", timeout=3)
            result["shutdown_request"] = "accepted"
        except Exception as exc:  # noqa: BLE001 - report only
            result["shutdown_request"] = f"failed: {exc}"
        took = engine.wait_exit(SHELL_STOP_BUDGET)
        if took is None:
            result["engine_exit"] = f"超过 {SHELL_STOP_BUDGET:.0f} 秒没退出，外壳强杀"
            engine.process.kill()
            engine.process.wait()
        else:
            result["engine_exit"] = f"{took:.1f} 秒后自己退出"
    elif name == "shell_gone":
        shell.kill()
        shell.wait()
        took = engine.wait_exit(60)
        if took is None:
            result["engine_exit"] = "外壳没了 60 秒后引擎仍在运行"
            engine.process.kill()
            engine.process.wait()
        else:
            result["engine_exit"] = f"外壳没了 {took:.1f} 秒后自己退出"
    elif name == "killed":
        # 外壳等满预算后的强杀，和 main.rs 的 stop_child 一样：Windows 上先 taskkill /T 连子程序一起结束。
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(engine.process.pid), "/T", "/F"],
                           capture_output=True, creationflags=NO_WINDOW, check=False)
        engine.process.kill()
        engine.process.wait()
        result["engine_exit"] = "被强杀"
    result["seconds_to_engine_gone"] = round(time.monotonic() - closed_at, 1)

    time.sleep(1)
    survivors = []
    for pid in child_pids:
        try:
            proc = psutil.Process(pid)
            if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                survivors.append(f"{pid} {proc.name()}")
        except psutil.Error:
            pass
    result["children_left_running"] = survivors
    result["engine_json_left"] = (app_data / "engine.json").exists()
    leftover = sorted(temp_entries() - before_temp)
    result["temp_left"] = leftover

    # 马上重新打开。
    reopen_start = time.monotonic()
    try:
        again = Engine(app_data, shell.pid if shell.poll() is None else os.getpid())
        result["reopen"] = f"{time.monotonic() - reopen_start:.1f} 秒后可用"
        task = again.api("GET", f"/api/tasks/{task_id}")
        result["task_state_after_reopen"] = task.get("state") if isinstance(task, dict) else task
        again.api("POST", "/api/system/shutdown")
        again.wait_exit(30)
    except Exception as exc:  # noqa: BLE001 - report only
        result["reopen"] = f"失败：{exc}"

    for pid in child_pids:
        try:
            psutil.Process(pid).kill()
        except psutil.Error:
            pass
    if shell.poll() is None:
        shell.kill()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="normal,shell_gone,killed,model_wait")
    parser.add_argument("--report", default="")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    workdir = Path(tempfile.mkdtemp(prefix="exit-probe-"))
    results = []
    for name in args.scenario.split(","):
        print(f"== {name}", flush=True)
        outcome = model_wait_scenario(workdir) if name.strip() == "model_wait" else scenario(name.strip(), workdir)
        print(json.dumps(outcome, ensure_ascii=False, indent=2), flush=True)
        results.append(outcome)
    if args.report:
        Path(args.report).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.strict:
        problems = []
        for outcome in results:
            name = outcome["scenario"]
            if outcome.get("error"):
                problems.append(f"{name}: {outcome['error']}")
            if outcome.get("children_left_running"):
                problems.append(f"{name}: 子程序还在运行 {outcome['children_left_running']}")
            if name == "model_wait" and outcome.get("seconds_to_engine_gone", 0) > 5:
                problems.append(f"{name}: 等模型回复的任务让关闭用了 {outcome.get('seconds_to_engine_gone')} 秒")
            if name != "killed" and outcome.get("temp_left"):
                problems.append(f"{name}: 临时文件夹残留 {outcome['temp_left']}")
            if not str(outcome.get("reopen", "")).endswith("可用"):
                problems.append(f"{name}: 重新打开 {outcome.get('reopen')}")
        for problem in problems:
            print(f"FAIL {problem}", flush=True)
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
