# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app_meta import APP_NAME, WINDOWS_PACKAGE_NAME  # noqa: E402

ICON_PATH = ROOT / "packaging" / "windows" / "assets" / "app-icon.ico"

datas = [
    (str(ROOT / "app_meta.py"), "."),
    (str(ROOT / "config.py"), "."),
    (str(ROOT / "settings.py"), "."),
    (str(ROOT / "assets"), "assets"),
]

metadata_packages = [
    "anthropic",
    "dashscope",
    "httpx",
    "loguru",
    "openai",
    "openpyxl",
    "Pillow",
    "psutil",
    "pydantic",
    "PyMuPDF",
    "PySide6_Essentials",
    "python-docx",
    "pywin32",
    "shiboken6",
    "tenacity",
    "xlrd",
    "xlwings",
    "zhipuai",
]
for package_name in metadata_packages:
    datas += copy_metadata(package_name)

hiddenimports = []
hiddenimports += collect_submodules("core")
hiddenimports += collect_submodules("engines")
hiddenimports += collect_submodules("native_app")
hiddenimports += [
    "anthropic",
    "dashscope",
    "httpx",
    "openai",
    "openpyxl",
    "docx",
    "PIL",
    "psutil",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtNetwork",
    "PySide6.QtWidgets",
    "pydantic",
    "pythoncom",
    "pywintypes",
    "loguru",
    "tenacity",
    "xlrd",
    "xlwings",
    "win32com",
    "win32com.client",
    "zhipuai",
]

a = Analysis(
    [str(ROOT / "scripts" / "launch_native.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["numpy", "pandas"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_PATH),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=WINDOWS_PACKAGE_NAME,
)
