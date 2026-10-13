"""真机检查「图纸转换工具」一键安装：从 ODA 官网真下载、核对签名、解开、试转换，再卸载。

在 macOS / Windows 上跑（PR checks 手动触发时由 CI 跑，也可以在本机跑）：

    python scripts/oda_install_smoke.py

用临时的应用数据目录，不碰本机已装的 ODA 和真实设置。成功退出 0。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _signer(executable: Path) -> str:
    """打出签名者，方便核对 oda_component_install.SIGNER 是否还对得上。"""
    if sys.platform == "darwin":
        app = next(parent for parent in executable.parents if parent.suffix == ".app")
        detail = subprocess.run(["codesign", "-dvv", str(app)], capture_output=True, text=True, check=False)
        return "\n".join(line for line in detail.stderr.splitlines() if line.startswith(("Authority=", "TeamIdentifier=")))
    return ""


def main() -> int:
    data_dir = tempfile.mkdtemp(prefix="oda-smoke-")
    os.environ["TRANSLATOR_APP_DATA_DIR"] = data_dir
    from api import cad_plugin
    from core import oda_component_install as oda

    print(f"platform: {oda.platform_key() or 'unsupported'}")
    if not oda.supported():
        print("one-click install is not supported here")
        return 1
    print(f"download: {oda.DOWNLOAD_URL.format(oda.package_name())}")
    last = [0.0]

    def progress(phase: str, done: int, total: int) -> None:
        now = time.monotonic()
        if phase != "download" or now - last[0] > 5:
            last[0] = now
            print(f"  {phase}: {done / 1048576:.1f} / {total / 1048576:.1f} MB" if phase == "download" else f"  {phase}")

    started = time.monotonic()
    try:
        info = oda.install(progress=progress)
    except oda.ComponentInstallError as exc:
        print(f"INSTALL FAILED: {exc}")
        return 1
    print(f"installed in {time.monotonic() - started:.0f}s: {info}")
    executable = oda.installed_executable()
    assert executable is not None
    print(_signer(executable))
    status = cad_plugin.probe_status()
    print(f"probe: oda={status['oda']} source={status['source']} version={status['converter_version']}")
    if status["oda"] != "connected" or status["source"] != "managed":
        print("PROBE FAILED")
        return 1
    status = cad_plugin.remove_managed()
    if status["managed_installed"] or oda.installed_executable() is not None:
        print("UNINSTALL FAILED")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
