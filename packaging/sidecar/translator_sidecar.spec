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
    excludes=[
        "PySide6", "PySide6_Essentials", "native_app", "numpy", "pandas", "shiboken6",
        # GUI toolkits nobody imports in this headless sidecar (already absent from the
        # build, kept as an explicit guard against future accidental pulls).
        "tkinter", "_tkinter", "tcl", "tk",
        # Packaging tooling not needed at runtime. pip/wheel were never collected in the
        # first place; setuptools (~40KB) was pulled in transitively but grepping every
        # reachable dependency (docx/openpyxl/xlwings/appscript/httpx/fastapi/starlette/
        # uvicorn/pydantic/psutil/h11/loguru/tenacity/xlrd/pypdfium2/anyio/click, plus our
        # own api/core/engines) found zero pkg_resources/setuptools imports. Verified via
        # full rebuild + frozen smoke test after excluding it.
        "pip", "wheel", "setuptools",
        # cryptography (~11MB, dominated by hazmat/bindings/_rust.abi3.so) is pulled in
        # solely because xlwings/pro/utils.py does `from cryptography.fernet import
        # Fernet` for its PRO license-key check, guarded by try/except ImportError. We
        # only use the free xlwings API (xw.App/xw.Book via the mac AppleScript engine),
        # never xlwings PRO features, so xlwings degrades to __pro__ = False cleanly.
        "cryptography",
        # AVIF is explicitly on the *unsupported* image format list in
        # core/pdf_image_translation.py (rejected before reaching PIL). Pillow's
        # Image.init() imports every *ImagePlugin submodule PyInstaller's own
        # hook-PIL.Image.py force-collects, but wraps each import in try/except
        # ImportError, so dropping this plugin (and its 2.9MB libavif.dylib + native
        # _avif extension, pulled in transitively once nothing imports the plugin) is
        # a clean no-op for every format we actually support (PNG/JPEG/WebP/BMP/TIFF).
        "PIL.AvifImagePlugin",
        # lxml is a hard runtime dependency of python-docx (lxml.etree, kept), but
        # PyInstaller's hook-lxml.py force-collects every lxml submodule via
        # collect_submodules('lxml') to work around pyinstaller/pyinstaller#5306. None
        # of python-docx/openpyxl/xlwings/appscript touch objectify/html/isoschematron/
        # sax/builder (grepped their installed sources directly) -- only lxml.etree and
        # its lxml._elementpath helper are actually used.
        "lxml.objectify", "lxml.html", "lxml.isoschematron", "lxml.sax", "lxml.builder",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# Symbol stripping is POSIX-only: PyInstaller's strip step shells out to the Unix
# `strip` utility, which Windows runners don't have (PyInstaller itself marks the
# option "not recommended on Windows").
#
# On macOS this is safe with signing: PyInstaller's COLLECT step already rewrites every
# collected binary's @rpath and unconditionally re-signs it afterwards (ad-hoc, since
# codesign_identity=None), regardless of whether strip is enabled -- see
# PyInstaller/building/utils.py's checkCache(), which always calls osxutils.sign_binary()
# after the strip/rpath-rewrite steps. So enabling strip does not introduce any new
# code-signing risk beyond what this build already does on every run. Verified with
# `codesign --verify --deep --strict` on the main executable plus a sample of stripped
# .so/.dylib files, and a full frozen smoke test, both passing.
STRIP_BINARIES = sys.platform != "win32"
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="translator-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=STRIP_BINARIES,
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
    strip=STRIP_BINARIES,
    upx=False,
    upx_exclude=[],
    name="translator-sidecar",
)
