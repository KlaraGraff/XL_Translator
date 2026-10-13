"""Build the no-Qt PyInstaller onedir sidecar used by the Tauri installer."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "packaging" / "sidecar" / "translator_sidecar.spec"
RESOURCE_PARENT = ROOT / "src-tauri" / "resources" / "sidecar"


def sidecar_executable(sidecar_dir: Path) -> Path:
    name = "translator-sidecar.exe" if sys.platform == "win32" else "translator-sidecar"
    return sidecar_dir / name


def build_sidecar(*, python: Path, output_parent: Path = RESOURCE_PARENT) -> Path:
    if not SPEC_PATH.is_file():
        raise FileNotFoundError(f"Missing PyInstaller spec: {SPEC_PATH}")

    target_dir = output_parent / "translator-sidecar"
    if target_dir.exists():
        shutil.rmtree(target_dir)
    output_parent.mkdir(parents=True, exist_ok=True)

    work_path = ROOT / ".runtime" / "package" / "pyinstaller-sidecar-work"
    command = [
        str(python),
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(output_parent),
        "--workpath",
        str(work_path),
        str(SPEC_PATH),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    executable = sidecar_executable(target_dir)
    if not executable.is_file():
        raise RuntimeError(f"PyInstaller did not produce the sidecar executable: {executable}")
    assert_pdf_component_not_bundled(target_dir)
    verify_pdf_component_loads(executable, python=python)
    run_frozen_smoke(executable)
    return executable


# PyMuPDF（AGPL-3.0，约 60MB）属于「PDF 处理组件」，不随安装包分发（见 core/pdf_component.py）。
PDF_COMPONENT_REQUIREMENT = "PyMuPDF>=1.24,<2"


def assert_pdf_component_not_bundled(sidecar_dir: Path) -> None:
    """spec 里排除了 fitz / pymupdf；构建机为了跑测试装着它，这里再查一遍产物。"""
    leaked = [
        path for path in sidecar_dir.rglob("*")
        if path.name.lower() in {"fitz", "pymupdf"} or path.name.lower().startswith(("pymupdf-", "libmupdf"))
    ]
    if leaked:
        names = ", ".join(str(path.relative_to(sidecar_dir)) for path in leaked[:5])
        raise RuntimeError(f"PyMuPDF must not be bundled into the sidecar, found: {names}")


def _pymupdf_site_dir(python: Path) -> Path | None:
    code = "import os, pymupdf; print(os.path.dirname(os.path.dirname(os.path.abspath(pymupdf.__file__))))"
    result = subprocess.run([str(python), "-c", code], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return Path(lines[-1].strip()) if lines else None


def verify_pdf_component_loads(executable: Path, *, python: Path) -> None:
    """用真的 PyMuPDF 让冻结后的 sidecar 走一遍插件加载（--probe-pdf-component）。

    冻结包只带我们自己代码用到的标准库；PyMuPDF 额外要的模块漏收时，用户连上组件
    才会出错。构建环境里没有 PyMuPDF 就临时装一份到 .runtime 下，用完即弃。
    """
    site_dir = _pymupdf_site_dir(python)
    if site_dir is None:
        site_dir = ROOT / ".runtime" / "package" / "pdf-component-probe"
        if site_dir.exists():
            shutil.rmtree(site_dir)
        subprocess.run(
            [str(python), "-m", "pip", "install", "--quiet", "--no-deps", "--target", str(site_dir),
             PDF_COMPONENT_REQUIREMENT],
            check=True,
        )
    result = subprocess.run(
        [str(executable), "--probe-pdf-component", str(site_dir)],
        capture_output=True, text=True, timeout=300, check=False,
    )
    output = (result.stdout or "").strip().splitlines()
    try:
        verdict = json.loads(output[-1]) if output else {}
    except ValueError:
        verdict = {}
    if result.returncode != 0 or not verdict.get("ok"):
        raise RuntimeError(
            "The frozen sidecar cannot load the PDF component (PyMuPDF): "
            f"{verdict.get('error') or (result.stderr or '').strip()[-800:]}"
        )
    print(f"[INFO] Frozen sidecar loads the PDF component: PyMuPDF {verdict.get('version', '')}")


def run_frozen_smoke(executable: Path) -> None:
    """冻结后的 sidecar 自检关键模块（scripts/frozen_smoke.py）：漏收的原生扩展（比如一键安装
    要用的 zstandard）在这里就报错，而不是等用户点安装时才暴露。"""
    result = subprocess.run([str(executable), "--smoke-test"], capture_output=True, text=True, timeout=300,
                            check=False)
    if result.returncode != 0:
        raise RuntimeError(f"The frozen sidecar smoke test failed: {(result.stderr or result.stdout).strip()[-800:]}")
    print("[INFO] Frozen sidecar smoke test passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--output-parent", type=Path, default=RESOURCE_PARENT)
    args = parser.parse_args()
    # Keep the virtualenv executable path intact.  Resolving its symlink turns
    # it into the base interpreter and makes PyInstaller unavailable.
    executable = build_sidecar(python=args.python.absolute(), output_parent=args.output_parent.resolve())
    print(executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
