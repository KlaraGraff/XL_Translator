from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
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

from app_meta import APP_NAME  # noqa: E402

try:
    from scripts.desktop_window import open_app_window
except ModuleNotFoundError:
    from desktop_window import open_app_window

BOOTSTRAP_MARKER = ".bootstrap_success"
MIN_CHECK_IMPORTS = ("streamlit", "openpyxl", "pandas", "webview")
MIN_PYTHON_VERSION = (3, 10)
MIN_PYTHON_VERSION_TEXT = ".".join(str(part) for part in MIN_PYTHON_VERSION)
PORT_START = 8501
PORT_END = 8510
SHUTDOWN_TIMEOUT_SECONDS = 2
FORCE_KILL_TIMEOUT_SECONDS = 2
PORT_RELEASE_TIMEOUT_SECONDS = 2
POST_START_PROBE_DELAY_SECONDS = 0.5
STARTUP_HEALTH_TIMEOUT_SECONDS = 8
STARTUP_STABILIZE_TIMEOUT_SECONDS = 2
WAIT_POLL_INTERVAL_SECONDS = 0.2
LAUNCH_SILENT = False
PYTHON_VERSION_CACHE: dict[str, tuple[int, int, int] | None] = {}


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def venv_python_candidates(root: Path) -> list[Path]:
    venv_root = root / ".venv"
    if os.name == "nt":
        return [
            venv_root / "Scripts" / "python.exe",
            venv_root / "Scripts" / "pythonw.exe",
        ]
    return [
        venv_root / "bin" / "python3",
        venv_root / "bin" / "python",
    ]


def bootstrap_python_candidates(root: Path) -> list[Path]:
    seen: set[str] = set()
    candidates: list[Path] = []

    def _append(candidate: str | Path | None) -> None:
        if candidate is None:
            return
        raw = str(candidate).strip()
        if not raw:
            return
        normalized = os.path.expanduser(raw)
        if normalized in seen:
            return
        seen.add(normalized)
        candidates.append(Path(normalized))

    _append(os.environ.get("PRODUCT_TRANSLATE_BOOTSTRAP_PYTHON"))
    _append(sys.executable)
    if os.name == "nt":
        for known_path in (
            root / "runtime" / "python" / "python.exe",
            root / "runtime" / "python" / "pythonw.exe",
        ):
            _append(known_path)
        for command_name in (
            "py",
            "python",
            "python3",
            "python3.13",
            "python3.12",
            "python3.11",
            "python3.10",
        ):
            _append(shutil.which(command_name))
        for candidate in venv_python_candidates(root):
            _append(candidate)
        return candidates

    for known_path in (
        "/opt/homebrew/bin/python3",
        "/opt/homebrew/bin/python3.13",
        "/opt/homebrew/bin/python3.12",
        "/opt/homebrew/bin/python3.11",
        "/opt/homebrew/bin/python3.10",
        "/usr/local/bin/python3",
        "/usr/local/bin/python3.13",
        "/usr/local/bin/python3.12",
        "/usr/local/bin/python3.11",
        "/usr/local/bin/python3.10",
        "/usr/bin/python3",
    ):
        _append(known_path)
    for command_name in ("python3", "python3.13", "python3.12", "python3.11", "python3.10"):
        _append(shutil.which(command_name))
    for candidate in venv_python_candidates(root):
        _append(candidate)
    return candidates


def python_version(candidate: Path) -> tuple[int, int, int] | None:
    cache_key = str(candidate)
    if cache_key in PYTHON_VERSION_CACHE:
        return PYTHON_VERSION_CACHE[cache_key]

    if not candidate.exists():
        PYTHON_VERSION_CACHE[cache_key] = None
        return None

    try:
        result = subprocess.run(
            [
                str(candidate),
                "-c",
                (
                    "import json, sys; "
                    "print(json.dumps(list(sys.version_info[:3])))"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
            **subprocess_creation_kwargs(),
        )
    except OSError:
        PYTHON_VERSION_CACHE[cache_key] = None
        return None

    if result.returncode != 0:
        PYTHON_VERSION_CACHE[cache_key] = None
        return None

    try:
        version_raw = json.loads((result.stdout or "").strip())
        version_info = tuple(int(part) for part in version_raw[:3])
    except (TypeError, ValueError, json.JSONDecodeError):
        PYTHON_VERSION_CACHE[cache_key] = None
        return None

    if len(version_info) != 3:
        PYTHON_VERSION_CACHE[cache_key] = None
        return None

    PYTHON_VERSION_CACHE[cache_key] = version_info
    return version_info


def is_supported_python(candidate: Path) -> bool:
    version_info = python_version(candidate)
    if version_info is None:
        return False
    return version_info >= MIN_PYTHON_VERSION


def format_python_version(version_info: tuple[int, int, int] | None) -> str:
    if version_info is None:
        return "unknown"
    return ".".join(str(part) for part in version_info)


def resolve_existing_path(
    candidates: list[Path],
    *,
    require_supported_python: bool = False,
) -> Path | None:
    for candidate in candidates:
        if not candidate.exists():
            continue
        if require_supported_python and not is_supported_python(candidate):
            continue
        if candidate.exists():
            return candidate
    return None


def venv_python(root: Path) -> Path:
    return venv_python_candidates(root)[0]


def bootstrap_python(root: Path) -> Path:
    return bootstrap_python_candidates(root)[0]


def bootstrap_marker(root: Path) -> Path:
    return root / ".venv" / BOOTSTRAP_MARKER


def runtime_dir(root: Path) -> Path:
    return root / ".runtime"


def instance_state_file(root: Path) -> Path:
    return runtime_dir(root) / "instance_state.json"


def launcher_log_file(root: Path) -> Path:
    return runtime_dir(root) / "launcher.log"


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"Launch the {APP_NAME} app.")
    parser.add_argument(
        "--silent",
        action="store_true",
        help="Run without showing console windows after bootstrap has completed.",
    )
    return parser.parse_args()


def subprocess_creation_kwargs() -> dict[str, int]:
    if LAUNCH_SILENT and os.name == "nt":
        return {
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        }
    return {}


def log(root: Path, message: str) -> None:
    line = f"[{now_ts()}] {message}"
    print(line)
    log_path = launcher_log_file(root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_cmd(cmd: list[str], cwd: Path, step_name: str) -> None:
    log(cwd, f"[INFO] {step_name}")
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        check=False,
        **subprocess_creation_kwargs(),
    )
    if result.returncode != 0:
        raise RuntimeError(f"{step_name} failed with exit code={result.returncode}")


def is_venv_ready(root: Path) -> bool:
    return (
        resolve_existing_path(
            venv_python_candidates(root),
            require_supported_python=True,
        )
        is not None
        and bootstrap_marker(root).exists()
    )


def remove_existing_venv(root: Path) -> None:
    venv_root = root / ".venv"
    if venv_root.exists():
        shutil.rmtree(venv_root)


def existing_venv_diagnostic(root: Path) -> str | None:
    existing_python = resolve_existing_path(venv_python_candidates(root))
    if existing_python is not None:
        version_info = python_version(existing_python)
        if version_info is None:
            return (
                f"Existing .venv interpreter could not be inspected: {existing_python}"
            )
        if version_info < MIN_PYTHON_VERSION:
            return (
                "Existing .venv uses unsupported Python "
                f"{format_python_version(version_info)} at {existing_python}"
            )

    marker = bootstrap_marker(root)
    if marker.exists() and existing_python is None:
        return "Bootstrap marker exists but the .venv interpreter is missing"

    return None


def create_venv(root: Path) -> None:
    base_python = (
        resolve_existing_path(
            bootstrap_python_candidates(root),
            require_supported_python=True,
        )
        or bootstrap_python(root)
    )
    run_cmd(
        [str(base_python), "-m", "venv", str(root / ".venv")],
        cwd=root,
        step_name="Create .venv",
    )


def install_dependencies(root: Path) -> None:
    py = (
        resolve_existing_path(
            venv_python_candidates(root),
            require_supported_python=True,
        )
        or venv_python(root)
    )
    requirements = root / "requirements.txt"
    run_cmd(
        [str(py), "-m", "pip", "install", "--upgrade", "pip"],
        cwd=root,
        step_name="Upgrade pip",
    )
    run_cmd(
        [str(py), "-m", "pip", "install", "-r", str(requirements)],
        cwd=root,
        step_name="Install requirements.txt",
    )


def verify_minimum_packages(root: Path) -> None:
    py = (
        resolve_existing_path(
            venv_python_candidates(root),
            require_supported_python=True,
        )
        or venv_python(root)
    )
    imports_expr = ", ".join(MIN_CHECK_IMPORTS)
    code = f"import {imports_expr}; print('ok')"
    run_cmd(
        [str(py), "-c", code],
        cwd=root,
        step_name=f"Verify required imports ({imports_expr})",
    )


def write_bootstrap_marker(root: Path) -> None:
    marker = bootstrap_marker(root)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("bootstrap_success\n", encoding="utf-8")


def ensure_bootstrapped(root: Path) -> None:
    if is_venv_ready(root):
        log(root, "[INFO] Existing bootstrap detected. Skipping first-run setup.")
        return

    marker = bootstrap_marker(root)
    diagnostic = existing_venv_diagnostic(root)
    if diagnostic is not None:
        log(
            root,
            "[WARN] "
            f"{diagnostic}. Recreating .venv with Python {MIN_PYTHON_VERSION_TEXT}+.",
        )
        remove_existing_venv(root)
    if marker.exists():
        marker.unlink()

    log(root, "[INFO] First-run bootstrap started.")
    create_venv(root)
    install_dependencies(root)
    verify_minimum_packages(root)
    write_bootstrap_marker(root)
    log(root, "[INFO] First-run bootstrap completed.")


def is_port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


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


def choose_available_port(excluded_ports: set[int] | None = None) -> int:
    excluded_ports = excluded_ports or set()
    for port in range(PORT_START, PORT_END + 1):
        if port in excluded_ports:
            continue
        if is_port_available(port):
            return port
    raise RuntimeError(
        f"No free port is available in the range {PORT_START}-{PORT_END}."
    )


def read_instance_state(root: Path) -> dict | None:
    state_path = instance_state_file(root)
    if not state_path.exists():
        return None

    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        log(root, "[WARN] Instance state file is unreadable. Ignoring it.")
        return None

    if not isinstance(data, dict):
        return None
    return data


def write_instance_state(root: Path, pid: int, port: int) -> None:
    state = {
        "pid": pid,
        "port": port,
        "project_root": str(root.resolve()),
        "created_at": now_ts(),
    }
    path = instance_state_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def remove_instance_state(root: Path) -> None:
    path = instance_state_file(root)
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
        if result.returncode != 0:
            return False
        output = (result.stdout or "").strip()
        if not output or "No tasks are running" in output:
            return False
        return f'"{pid}"' in output

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def get_process_commandline(pid: int) -> str:
    if os.name == "nt":
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\"; "
                    "if($p){$p.CommandLine}else{''}"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
            **subprocess_creation_kwargs(),
        )
        if result.returncode != 0:
            return ""
        return (result.stdout or "").strip()

    result = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
        **subprocess_creation_kwargs(),
    )
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def is_owned_streamlit_instance(root: Path, pid: int) -> bool:
    if not is_process_alive(pid):
        return False

    command_line = get_process_commandline(pid).lower()
    if not command_line:
        return False

    root_text = str(root.resolve()).lower()
    app_text = str((root / "app.py").resolve()).lower()
    return (
        "streamlit" in command_line
        and "run" in command_line
        and root_text in command_line
        and app_text in command_line
    )


def terminate_pid(pid: int, force: bool = False) -> None:
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

    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        return


def wait_process_exit(pid: int, timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(WAIT_POLL_INTERVAL_SECONDS)
    return not is_process_alive(pid)


def wait_port_release(port: int, timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if is_port_available(port):
            return True
        time.sleep(WAIT_POLL_INTERVAL_SECONDS)
    return is_port_available(port)


def cleanup_previous_instance(root: Path) -> None:
    state = read_instance_state(root)
    if not state:
        return

    pid = int(state.get("pid", 0) or 0)
    port = int(state.get("port", 0) or 0)

    if pid <= 0:
        log(root, "[WARN] Found an invalid PID in instance_state.json. Cleaning it up.")
        remove_instance_state(root)
        return

    if not is_owned_streamlit_instance(root, pid):
        log(root, "[WARN] Stored PID is not a live app instance for this project. Cleaning it up.")
        remove_instance_state(root)
        return

    log(root, f"[INFO] Stopping previous app instance PID={pid}.")
    terminate_pid(pid, force=False)

    if not wait_process_exit(pid, SHUTDOWN_TIMEOUT_SECONDS):
        log(root, f"[WARN] PID={pid} did not exit in time. Escalating to force kill.")
        terminate_pid(pid, force=True)

    if not wait_process_exit(pid, FORCE_KILL_TIMEOUT_SECONDS):
        raise RuntimeError(
            f"Unable to stop the previous app instance PID={pid}. Please close it manually."
        )

    if port > 0 and not wait_port_release(port, PORT_RELEASE_TIMEOUT_SECONDS):
        raise RuntimeError(f"Previous app port {port} did not release in time.")

    log(root, "[INFO] Previous app instance stopped.")
    remove_instance_state(root)


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


def wait_process_stable(process: subprocess.Popen, timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        time.sleep(WAIT_POLL_INTERVAL_SECONDS)
    return process.poll() is None


def start_streamlit_once(root: Path, port: int) -> int:
    py = (
        resolve_existing_path(
            venv_python_candidates(root),
            require_supported_python=True,
        )
        or venv_python(root)
    )
    app = root / "app.py"
    url = f"http://127.0.0.1:{port}"
    popen_kwargs: dict[str, object] = {"cwd": str(root)}
    popen_kwargs.update(subprocess_creation_kwargs())

    process_log_handle = None
    process: subprocess.Popen | None = None
    if LAUNCH_SILENT:
        log_path = launcher_log_file(root)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        process_log_handle = log_path.open("a", encoding="utf-8", buffering=1)
        popen_kwargs["stdout"] = process_log_handle
        popen_kwargs["stderr"] = subprocess.STDOUT

    log(root, f"[INFO] Using port: {port}")
    try:
        process = subprocess.Popen(
            [
                str(py),
                "-m",
                "streamlit",
                "run",
                str(app),
                "--server.address",
                "127.0.0.1",
                "--server.port",
                str(port),
                "--server.headless",
                "true",
                "--browser.gatherUsageStats",
                "false",
            ],
            **popen_kwargs,
        )

        write_instance_state(root, process.pid, port)
        log(root, f"[INFO] Recorded app instance PID={process.pid}")

        time.sleep(POST_START_PROBE_DELAY_SECONDS)
        if process.poll() is not None:
            remove_instance_state(root)
            raise RuntimeError(
                "Streamlit exited immediately. Check .runtime/launcher.log for details."
            )

        if not wait_streamlit_ready(port, STARTUP_HEALTH_TIMEOUT_SECONDS):
            remove_instance_state(root)
            terminate_pid(process.pid, force=True)
            raise RuntimeError(
                "Streamlit did not become healthy in time. Check .runtime/launcher.log."
            )

        if not wait_process_stable(process, STARTUP_STABILIZE_TIMEOUT_SECONDS):
            remove_instance_state(root)
            raise RuntimeError(
                "Streamlit exited during startup stabilization. "
                "The selected port may have been occupied by another instance."
            )

        managed_window = open_app_window(url, log_callback=lambda message: log(root, f"[INFO] {message}"))
        if managed_window:
            log(root, "[INFO] App window closed. Stopping Streamlit process.")
            if process.poll() is None:
                terminate_pid(process.pid, force=False)
                if not wait_process_exit(process.pid, SHUTDOWN_TIMEOUT_SECONDS):
                    terminate_pid(process.pid, force=True)
                    if not wait_process_exit(process.pid, FORCE_KILL_TIMEOUT_SECONDS):
                        raise RuntimeError(
                            f"Unable to stop Streamlit after app window closed. PID={process.pid}"
                        )
            exit_code = process.poll()
            remove_instance_state(root)
            log(root, f"[INFO] Streamlit process exited with code={exit_code}")
            return 0

        if LAUNCH_SILENT:
            log(root, "[INFO] App started in silent mode.")
        else:
            log(root, "[INFO] App started. Press Ctrl+C to stop the server.")

        exit_code = process.wait()
        remove_instance_state(root)
        log(root, f"[INFO] Streamlit process exited with code={exit_code}")
        return exit_code
    except Exception:
        if process is not None and process.poll() is None:
            terminate_pid(process.pid, force=True)
            wait_process_exit(process.pid, FORCE_KILL_TIMEOUT_SECONDS)
        remove_instance_state(root)
        raise
    finally:
        if process_log_handle is not None:
            process_log_handle.close()


def start_streamlit(root: Path) -> int:
    last_error: RuntimeError | None = None
    attempted_ports: list[int] = []
    excluded_ports: set[int] = set()

    for _ in range(PORT_END - PORT_START + 1):
        port = choose_available_port(excluded_ports)
        attempted_ports.append(port)
        try:
            return start_streamlit_once(root, port)
        except RuntimeError as exc:
            last_error = exc
            excluded_ports.add(port)
            log(root, f"[WARN] Startup attempt on port {port} failed: {exc}")

    attempted_text = ", ".join(str(port) for port in attempted_ports)
    detail = str(last_error) if last_error is not None else "unknown startup failure"
    raise RuntimeError(
        "Unable to start Streamlit on any available port in the configured range. "
        f"Tried: {attempted_text}. Last error: {detail}"
    )


def main() -> int:
    global LAUNCH_SILENT

    args = parse_args()
    LAUNCH_SILENT = args.silent

    root = project_root()

    try:
        if LAUNCH_SILENT and not is_venv_ready(root):
            raise RuntimeError(
                "Silent launch is only available after the first visible bootstrap "
                "has completed."
            )

        if not is_venv_ready(root):
            bootstrap_py = resolve_existing_path(
                bootstrap_python_candidates(root),
                require_supported_python=True,
            )
            if bootstrap_py is None:
                raise RuntimeError(
                    f"Python {MIN_PYTHON_VERSION_TEXT}+ is required for the first launch bootstrap. "
                    "Please install a newer python3 or set PRODUCT_TRANSLATE_BOOTSTRAP_PYTHON."
                )
            log(root, f"[INFO] Bootstrap Python resolved to: {bootstrap_py}")

        ensure_bootstrapped(root)
        cleanup_previous_instance(root)
        return start_streamlit(root)
    except Exception as exc:  # noqa: BLE001
        log(root, f"[ERROR] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
