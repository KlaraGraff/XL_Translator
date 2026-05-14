# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app_meta import APP_VERSION  # noqa: E402

ICON_PATH = ROOT / "packaging" / "macos" / "assets" / "app-icon.icns"

datas = [
    (str(ROOT / "app.py"), "."),
    (str(ROOT / "app_meta.py"), "."),
    (str(ROOT / "config.py"), "."),
    (str(ROOT / "settings.py"), "."),
    (str(ROOT / "assets"), "assets"),
    (str(ROOT / "ui" / "styles.css"), "ui"),
    (str(ROOT / ".streamlit"), ".streamlit"),
]
datas += collect_data_files("streamlit")
datas += collect_data_files("streamlit_extras")

hiddenimports = []
hiddenimports += collect_submodules("core")
hiddenimports += collect_submodules("engines")
hiddenimports += collect_submodules("ui")
hiddenimports += collect_submodules("streamlit")
hiddenimports += collect_submodules("streamlit_extras")
hiddenimports += collect_submodules("altair")
hiddenimports += collect_submodules("pyarrow")
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
    "pydantic",
    "tenacity",
    "xlrd",
    "xlwings",
    "zhipuai",
]

a = Analysis(
    [str(ROOT / "scripts" / "frozen_launcher.py")],
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
    name="XL Translator",
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
    name="XL Translator",
)

app = BUNDLE(
    coll,
    name="XL Translator.app",
    icon=str(ICON_PATH),
    bundle_identifier="com.klara-graff.xl-translator",
    info_plist={
        "CFBundleDisplayName": "XL Translator",
        "CFBundleName": "XL Translator",
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION,
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
    },
)
