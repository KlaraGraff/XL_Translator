# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app_meta import (  # noqa: E402
    APP_BUNDLE_IDENTIFIER,
    APP_NAME,
    APP_VERSION,
    MACOS_APP_BUNDLE_NAME,
    MACOS_COLLECT_NAME,
)

ICON_PATH = ROOT / "packaging" / "macos" / "assets" / "app-icon.icns"

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
    "pandas",
    "Pillow",
    "psutil",
    "pydantic",
    "PySide6_Essentials",
    "python-docx",
    "python-dotenv",
    "rich",
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
    "docx",
    "dotenv",
    "httpx",
    "loguru",
    "openai",
    "openpyxl",
    "pandas",
    "PIL",
    "psutil",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "pydantic",
    "tenacity",
    "xlrd",
    "xlwings",
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
    excludes=[],
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
    upx=False,
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
    upx=False,
    upx_exclude=[],
    name=MACOS_COLLECT_NAME,
)

app = BUNDLE(
    coll,
    name=MACOS_APP_BUNDLE_NAME,
    icon=str(ICON_PATH),
    bundle_identifier=APP_BUNDLE_IDENTIFIER,
    info_plist={
        "CFBundleDisplayName": APP_NAME,
        "CFBundleName": APP_NAME,
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION,
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
    },
)
