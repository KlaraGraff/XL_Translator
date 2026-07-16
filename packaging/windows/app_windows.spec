# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules, copy_metadata

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
    "httpx",
    "loguru",
    "openpyxl",
    "Pillow",
    "pypdfium2",
    "psutil",
    "pydantic",
    "PySide6_Essentials",
    "python-docx",
    "pywin32",
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
    "httpx",
    "openpyxl",
    "docx",
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
    "pythoncom",
    "pywintypes",
    "loguru",
    "tenacity",
    "xlrd",
    "xlwings",
    "win32com",
    "win32com.client",
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
        "PySide6/Qt/plugins/imageformats/qgif",
        "PySide6/Qt/plugins/imageformats/qicns",
        "PySide6/Qt/plugins/imageformats/qico",
        "PySide6/Qt/plugins/imageformats/qjpeg",
        "PySide6/Qt/plugins/imageformats/qmacheif",
        "PySide6/Qt/plugins/imageformats/qmacjp2",
        "PySide6/Qt/plugins/imageformats/qtga",
        "PySide6/Qt/plugins/imageformats/qtiff",
        "PySide6/Qt/plugins/imageformats/qwbmp",
        "PySide6/Qt/plugins/imageformats/qwebp",
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
    pruned_binaries,
    pruned_datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=WINDOWS_PACKAGE_NAME,
)
