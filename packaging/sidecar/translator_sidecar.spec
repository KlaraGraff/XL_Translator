# -*- mode: python ; coding: utf-8 -*-

"""PyInstaller onedir build for the headless Translator engine sidecar."""

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

datas = [
    (str(ROOT / "app_meta.py"), "."),
    (str(ROOT / "config.py"), "."),
    (str(ROOT / "settings.py"), "."),
    (str(ROOT / "assets"), "assets"),
]

metadata_packages = [
    "fastapi",
    "h11",
    "httpx",
    "loguru",
    "openpyxl",
    "Pillow",
    "pydantic",
    "pypdfium2",
    "psutil",
    "python-docx",
    "starlette",
    "tenacity",
    "uvicorn",
    "xlrd",
    "xlwings",
]
for package_name in metadata_packages:
    datas += copy_metadata(package_name)

hiddenimports = []
hiddenimports += collect_submodules("api")
hiddenimports += collect_submodules("core")
hiddenimports += collect_submodules("engines")
hiddenimports += [
    "docx",
    "httpx",
    "loguru",
    "openpyxl",
    "PIL",
    "pypdfium2",
    "pypdfium2.raw",
    "pypdfium2_raw",
    "psutil",
    "pydantic",
    "tenacity",
    "uvicorn",
    "xlrd",
    "xlwings",
]
if sys.platform == "win32":
    hiddenimports += ["pythoncom", "pywintypes", "win32com", "win32com.client"]

binaries = collect_dynamic_libs("pypdfium2_raw")

a = Analysis(
    [str(ROOT / "scripts" / "launch_sidecar.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PySide6", "PySide6_Essentials", "native_app", "numpy", "pandas", "shiboken6"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="translator-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="translator-sidecar",
)
