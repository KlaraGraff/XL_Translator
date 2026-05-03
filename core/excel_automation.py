from __future__ import annotations

import platform
from typing import Any

import psutil


SUPPORTED_LOCAL_EXCEL_PLATFORMS = {"Darwin"}


def local_excel_platform() -> str:
    return platform.system()


def supports_local_excel_automation() -> bool:
    return local_excel_platform() in SUPPORTED_LOCAL_EXCEL_PLATFORMS


def initialize_excel_thread() -> Any | None:
    """macOS 不需要额外的线程级 Excel 初始化。"""
    return None


def finalize_excel_thread(thread_state: Any | None) -> None:
    _ = thread_state


def create_excel_app(*, visible: bool = False, add_book: bool = False):
    import xlwings as xw

    return xw.App(visible=visible, add_book=add_book)


def get_excel_process_pid(app) -> int | None:
    pid = getattr(app, "pid", None)
    if isinstance(pid, int) and pid > 0:
        return pid
    return None


def probe_local_excel_automation() -> tuple[bool, str]:
    """Probe whether local Excel automation is actually usable on this machine."""
    if not supports_local_excel_automation():
        return False, f"当前平台 {local_excel_platform()} 暂不支持本地 Excel 自动化。"

    thread_state = initialize_excel_thread()
    app = None
    try:
        try:
            app = create_excel_app(visible=False, add_book=False)
        except ImportError:
            return False, "未安装 xlwings，无法连接本地 Excel。"
        except Exception as exc:
            return False, f"无法启动本地 Excel：{exc}"

        try:
            app.display_alerts = False
        except Exception:
            pass
        return True, ""
    finally:
        if app is not None:
            try:
                app.quit()
            except Exception:
                try:
                    app.kill()
                except Exception:
                    pass
        finalize_excel_thread(thread_state)


def terminate_process_tree(pid: int | None, *, force: bool = False, timeout: float = 3.0) -> bool:
    """Terminate a process tree by PID on any supported desktop platform."""
    if not pid:
        return False

    try:
        root = psutil.Process(pid)
    except psutil.Error:
        return True

    processes = root.children(recursive=True)
    processes.append(root)

    unique_processes: list[psutil.Process] = []
    seen_pids: set[int] = set()
    for proc in processes:
        if proc.pid in seen_pids:
            continue
        seen_pids.add(proc.pid)
        unique_processes.append(proc)

    for proc in reversed(unique_processes):
        try:
            if force:
                proc.kill()
            else:
                proc.terminate()
        except psutil.Error:
            continue

    _, alive = psutil.wait_procs(unique_processes, timeout=timeout)
    if not alive:
        return True

    if force:
        return False

    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            continue

    _, still_alive = psutil.wait_procs(alive, timeout=timeout)
    return not still_alive
