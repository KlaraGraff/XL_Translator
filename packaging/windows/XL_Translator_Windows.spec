# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parents[1]
ICON_PATH = ROOT / "packaging" / "windows" / "assets" / "app-icon.ico"

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

metadata_packages = [
    "anthropic",
    "altair",
    "dashscope",
    "httpx",
    "loguru",
    "openai",
    "openpyxl",
    "pandas",
    "Pillow",
    "psutil",
    "pyarrow",
    "pydantic",
    "python-docx",
    "python-dotenv",
    "rich",
    "streamlit",
    "streamlit-extras",
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
hiddenimports += collect_submodules("ui")
hiddenimports += collect_submodules("streamlit")
hiddenimports += collect_submodules("streamlit_extras")
hiddenimports += collect_submodules("altair")
hiddenimports += collect_submodules("pyarrow")
hiddenimports += [
    "anthropic",
    "dashscope",
    "httpx",
    "openai",
    "openpyxl",
    "pandas",
    "docx",
    "PIL",
    "psutil",
    "pydantic",
    "loguru",
    "dotenv",
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
    name="XL_Translator_Windows",
)
