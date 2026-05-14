"""Frozen desktop launcher for PyInstaller builds.

The launcher starts the packaged Streamlit app on a local port and opens an
in-app desktop window. It is intentionally thin: product logic stays in the
shared app/core/ui modules.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import app as _app  # noqa: E402,F401 - imported so PyInstaller collects app modules
try:
    from scripts.desktop_window import open_app_window
except ModuleNotFoundError:
    from desktop_window import open_app_window

PORT_START = 8501
PORT_END = 8510
WAIT_POLL_INTERVAL_SECONDS = 0.2
STARTUP_HEALTH_TIMEOUT_SECONDS = 12
POST_START_PROBE_DELAY_SECONDS = 0.5


def bundle_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return Path(__file__).resolve().parents[1]


def app_script_path() -> Path:
    return bundle_root() / "app.py"


def state_root() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "XL Translator"
    return Path.home() / ".xl_translator"


def state_file() -> Path:
    return state_root() / "desktop_instance.json"


def log_file() -> Path:
    return state_root() / "desktop_launcher.log"


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    line = f"[{now_ts()}] {message}"
    log_path = log_file()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch packaged XL Translator.")
    parser.add_argument("--streamlit-child", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    return parser.parse_args()


def is_port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def choose_available_port(excluded_ports: set[int] | None = None) -> int:
    excluded_ports = excluded_ports or set()
    for port in range(PORT_START, PORT_END + 1):
        if port in excluded_ports:
            continue
        if is_port_available(port):
            return port
    raise RuntimeError(f"No free port is available in {PORT_START}-{PORT_END}.")


def is_streamlit_responding(port: int, timeout: float = 1.5) -> bool:
    url = f"http://127.0.0.1:{port}/_stcore/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def is_streamlit_ui_responding(port: int, timeout: float = 1.5) -> bool:
    url = f"http://127.0.0.1:{port}/"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as exc:
        return 200 <= exc.code < 400
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def wait_streamlit_ready(port: int, timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if (
            is_streamlit_responding(port, timeout=0.8)
            and is_streamlit_ui_responding(port, timeout=0.8)
        ):
            return True
        time.sleep(WAIT_POLL_INTERVAL_SECONDS)
    return (
        is_streamlit_responding(port, timeout=0.8)
        and is_streamlit_ui_responding(port, timeout=0.8)
    )


def read_state() -> dict | None:
    path = state_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def write_state(pid: int, port: int) -> None:
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "port": port,
                "created_at": now_ts(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def remove_state() -> None:
    path = state_file()
    if path.exists():
        path.unlink()


def is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
            **subprocess_creation_kwargs(),
        )
        output = (result.stdout or "").strip()
        return result.returncode == 0 and bool(output) and f'"{pid}"' in output
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_pid(pid: int, force: bool = False) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        cmd = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            cmd.append("/F")
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            **subprocess_creation_kwargs(),
        )
        return
    try:
        os.kill(pid, 9 if force else 15)
    except OSError:
        return


def cleanup_previous_instance() -> None:
    state = read_state()
    if not state:
        return
    pid = int(state.get("pid", 0) or 0)
    port = int(state.get("port", 0) or 0)
    if pid <= 0:
        remove_state()
        return
    if is_process_alive(pid):
        terminate_pid(pid, force=False)
        time.sleep(0.8)
        if is_process_alive(pid):
            terminate_pid(pid, force=True)
    if port > 0:
        deadline = time.time() + 3
        while time.time() < deadline and not is_port_available(port):
            time.sleep(WAIT_POLL_INTERVAL_SECONDS)
    remove_state()


def subprocess_creation_kwargs() -> dict[str, int]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def run_streamlit_child(port: int) -> int:
    app_path = app_script_path()
    if not app_path.exists():
        raise RuntimeError(f"Missing packaged app.py: {app_path}")

    from streamlit.web import bootstrap

    flag_options = {
        "global.developmentMode": False,
        "server.address": "127.0.0.1",
        "server.port": port,
        "server.headless": True,
        "browser.gatherUsageStats": False,
    }
    bootstrap.load_config_options(flag_options)
    bootstrap.run(str(app_path), False, [], flag_options)
    return 0


def start_desktop_app() -> int:
    cleanup_previous_instance()
    port = choose_available_port()
    url = f"http://127.0.0.1:{port}"
    log(f"Starting packaged Streamlit app on port {port}")

    process = subprocess.Popen(
        [
            sys.executable,
            "--streamlit-child",
            "--port",
            str(port),
        ],
        cwd=str(bundle_root()),
        **subprocess_creation_kwargs(),
    )
    write_state(process.pid, port)

    time.sleep(POST_START_PROBE_DELAY_SECONDS)
    if process.poll() is not None:
        remove_state()
        raise RuntimeError("Packaged Streamlit process exited immediately.")

    if not wait_streamlit_ready(port, STARTUP_HEALTH_TIMEOUT_SECONDS):
        terminate_pid(process.pid, force=True)
        remove_state()
        raise RuntimeError("Packaged Streamlit process did not become healthy in time.")

    managed_window = open_app_window(url, log_callback=log)
    if managed_window:
        log("App window closed. Stopping packaged Streamlit process.")
        if process.poll() is None:
            terminate_pid(process.pid, force=False)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                terminate_pid(process.pid, force=True)
                process.wait(timeout=3)
        exit_code = process.wait()
        remove_state()
        log(f"Packaged Streamlit process exited with code={exit_code}")
        return 0

    exit_code = process.wait()
    remove_state()
    log(f"Packaged Streamlit process exited with code={exit_code}")
    return int(exit_code or 0)


def main() -> int:
    args = parse_args()
    try:
        if args.streamlit_child:
            return run_streamlit_child(args.port)
        return start_desktop_app()
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
