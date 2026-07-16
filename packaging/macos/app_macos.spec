# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import os
import sys

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app_meta import (  # noqa: E402
    APP_BUNDLE_IDENTIFIER,
    APP_NAME,
    APP_VERSION,
    MACOS_APP_BUNDLE_NAME,
    MACOS_COLLECT_NAME,
    MACOS_MINIMUM_SYSTEM_VERSION as DEFAULT_MACOS_MINIMUM_SYSTEM_VERSION,
)

ICON_PATH = ROOT / "packaging" / "macos" / "assets" / "app-icon.icns"
MACOS_MINIMUM_SYSTEM_VERSION = os.environ.get(
    "XL_TRANSLATOR_MACOS_MINIMUM_SYSTEM_VERSION",
    DEFAULT_MACOS_MINIMUM_SYSTEM_VERSION,
).strip()

datas = [
    (str(ROOT / "app_meta.py"), "."),
    (str(ROOT / "config.py"), "."),
    (str(ROOT / "settings.py"), "."),
    (str(ROOT / "assets"), "assets"),
]

metadata_packages = [
    "httpx",
    "loguru",
    "openpyxl",
    "Pillow",
    "pypdfium2",
    "psutil",
    "pydantic",
    "PySide6_Essentials",
    "python-docx",
    "shiboken6",
    "tenacity",
    "xlrd",
    "xlwings",
]
for package_name in metadata_packages:
    datas += copy_metadata(package_name)

hiddenimports = []
hiddenimports += collect_submodules("core")
hiddenimports += collect_submodules("engines")
hiddenimports += collect_submodules("native_app")
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
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtNetwork",
    "PySide6.QtWidgets",
    "pydantic",
    "tenacity",
    "xlrd",
    "xlwings",
]

binaries = collect_dynamic_libs("pypdfium2_raw")

a = Analysis(
    [str(ROOT / "scripts" / "launch_native.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["numpy", "pandas"],
    noarchive=False,
    optimize=0,
)


def _exclude_unused_qt_bundle_entries(entries, prefixes):
    normalized_prefixes = tuple(prefix.replace("\\", "/") for prefix in prefixes)
    return [
        entry
        for entry in entries
        if not str(entry[0]).replace("\\", "/").startswith(normalized_prefixes)
    ]


pruned_binaries = _exclude_unused_qt_bundle_entries(
    a.binaries,
    (
        "PySide6/QtDBus.",
        "PySide6/Qt/plugins/generic/",
        "PySide6/Qt/plugins/networkinformation/",
        "PySide6/Qt/plugins/imageformats/libqgif",
        "PySide6/Qt/plugins/imageformats/libqicns",
        "PySide6/Qt/plugins/imageformats/libqico",
        "PySide6/Qt/plugins/imageformats/libqjpeg",
        "PySide6/Qt/plugins/imageformats/libqmacheif",
        "PySide6/Qt/plugins/imageformats/libqmacjp2",
        "PySide6/Qt/plugins/imageformats/libqtga",
        "PySide6/Qt/plugins/imageformats/libqtiff",
        "PySide6/Qt/plugins/imageformats/libqwbmp",
        "PySide6/Qt/plugins/imageformats/libqwebp",
    ),
)
pruned_datas = _exclude_unused_qt_bundle_entries(
    a.datas,
    ("PySide6/Qt/translations/",),
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
    pruned_binaries,
    pruned_datas,
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
        "LSMinimumSystemVersion": MACOS_MINIMUM_SYSTEM_VERSION,
        "NSHighResolutionCapable": True,
    },
)
