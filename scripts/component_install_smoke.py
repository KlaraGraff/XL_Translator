"""真机检查「PDF 处理组件」「文字识别组件」一键安装：真下载、核对、解压、试运行，再卸载。

在 macOS / Windows 上跑（PR checks 手动触发时由 CI 跑，也可以在本机跑）：

    python scripts/component_install_smoke.py [pdf] [ocr]

用临时的应用数据目录（设了 TRANSLATOR_APP_DATA_DIR 时用它），不碰本机已装的组件和真实设置。
都成功时退出 0。
"""
from __future__ import annotations

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
    installer.uninstall()
    return installer.installed_executable() is None


def main(argv: list[str]) -> int:
    # Windows 控制台默认 cp1252，打不出中文报错。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if not os.environ.get("TRANSLATOR_APP_DATA_DIR"):
        os.environ["TRANSLATOR_APP_DATA_DIR"] = tempfile.mkdtemp(prefix="component-smoke-")
    print(f"data dir: {os.environ['TRANSLATOR_APP_DATA_DIR']}")
    checks = {"pdf": _pdf, "ocr": _ocr}
    names = [name for name in argv if name in checks] or list(checks)
    results = {name: checks[name]() for name in names}
    print("RESULT: " + ", ".join(f"{name}={'OK' if ok else 'FAILED'}" for name, ok in results.items()))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
