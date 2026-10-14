"""Entrypoint used by the frozen Python translation-engine sidecar."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _run() -> int:
    if len(sys.argv) >= 4 and sys.argv[1] == "--adapter-host":
        # 用户安装的扩展在子进程里跑一次 scan / extract / write（见 core.adapter_subprocess_host）。
        from core.adapter_subprocess_host import host_main

        return host_main(sys.argv[2], sys.argv[3])

    if len(sys.argv) >= 2:
        # 命令行入口（types / scan / translate / status / stop，以及给智能体用的 mcp）：
        # 连上正在运行的应用，或在后台起一个引擎（cli/）。不带子命令时照旧作为翻译引擎运行。
        from cli.main import COMMANDS

        if sys.argv[1] in COMMANDS:
            from cli.main import main as cli_main

            return cli_main(sys.argv[1:])

    if len(sys.argv) >= 3 and sys.argv[1] == "--probe-pdf-component":
        # 检测「PDF 处理组件」时在子进程里试加载 PyMuPDF（见 core.pdf_component）。
        from core.pdf_component import probe_main

        return probe_main(sys.argv[2], convert="--convert" in sys.argv[3:])

    if len(sys.argv) >= 5 and sys.argv[1] == "--pdf2docx-convert":
        # 一键安装的组件把 PDF 转成 Word：NumPy、OpenCV 只进这个子进程。
        from core.pdf_component import convert_main

        return convert_main(sys.argv[2], sys.argv[3], sys.argv[4])

    if "--smoke-test" in sys.argv[1:]:
        from scripts.frozen_smoke import run_smoke_test

        return run_smoke_test()

    from api.launcher import main

    main()
    return 0


if __name__ == "__main__":
    raise SystemExit(_run())
