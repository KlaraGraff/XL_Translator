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
    # 原位替换判断文字识别结果靠这两份词表（core/pdf_vector/classify.py）。漏收时不报错，
    # 只是每一处识别出的字都「查不到」：旋转的字一律不替换，横排的也大多保留原文。
    (str(ROOT / "core" / "pdf_vector" / "lexicon" / "eng.words.txt.gz"), "core/pdf_vector/lexicon"),
    (str(ROOT / "core" / "pdf_vector" / "lexicon" / "fra.words.txt.gz"), "core/pdf_vector/lexicon"),
    # 繁體中文译文里漏出的简体字靠这份 OpenCC 字词表改成繁体（core/zh_hant）。漏收时目标语言
    # 选繁体就会报错找不到文件。
    (str(ROOT / "core" / "zh_hant" / "opencc_s2twp.txt.gz"), "core/zh_hant"),
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
    "zstandard",
]
for package_name in metadata_packages:
    datas += copy_metadata(package_name)

# 随应用提供的翻译类型（core/format_adapter.py BUNDLED_ADAPTERS_DIR）：每个类型只收
# adapter.json 与 adapter.py，samples/ 只给测试用。漏收时应用照常启动，只是侧栏少了这些类型。
for adapter_file in sorted((ROOT / "adapters").glob("*/adapter.*")):
    datas.append((str(adapter_file), f"adapters/{adapter_file.parent.name}"))

# `adapter new` 生成扩展用的模板（与模板仓库 XL_Translator-extension-template 相同的全部文件，
# 含 .github/ 和 .gitignore），`adapter pack` 用其中的 scripts/pack.py。冻结冒烟会生成、自测并打包一次。
_ADAPTER_TEMPLATE = ROOT / "cli" / "adapter_template"
for template_file in sorted(_ADAPTER_TEMPLATE.rglob("*")):
    if template_file.is_file() and "__pycache__" not in template_file.parts:
        relative_dir = template_file.parent.relative_to(_ADAPTER_TEMPLATE).as_posix()
        datas.append((str(template_file), "cli/adapter_template" + ("" if relative_dir == "." else f"/{relative_dir}")))

hiddenimports = []
hiddenimports += collect_submodules("api")
hiddenimports += collect_submodules("core")
hiddenimports += collect_submodules("engines")
# 格式适配器要 import 的协议包（随应用提供的适配器从 adapters/ 按文件加载，见 core/format_adapter.py）。
hiddenimports += collect_submodules("translator_adapter")
# 命令行子命令（types / scan / translate / status / stop / mcp），见 scripts/launch_sidecar.py。
hiddenimports += collect_submodules("cli")
# mcp 子命令（cli/mcp_server.py）用的 SDK 服务端，和冻结冒烟握手用的客户端。不整包收：
# mcp.cli 要 typer，mcp.server.fastmcp 在 2.x 里一导入就报错。
hiddenimports += [
    "mcp.server.mcpserver",
    "mcp.server.stdio",
    "mcp.client.stdio",
]
hiddenimports += [
    "docx",
    "httpx",
    # 系统代理是 SOCKS 时 httpx 才去 import 它；漏收时冻结版一遇到 SOCKS 代理就只能直连。
    "socksio",
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
    # 一键安装「文字识别组件」时解压 conda-forge 的 .conda 包。
    "zstandard",
    "zstandard.backend_c",
]
# 「PDF 处理组件」的 PyMuPDF 不在安装包里，运行时才从用户的 pdf2docx 环境加载
# （core/pdf_component.py）。它 import 的标准库模块得在包里：我们自己的代码没用到的那几个，
# PyInstaller 不会自己收。构建工作流用真的 PyMuPDF 跑一遍 --probe-pdf-component 把关。
hiddenimports += [
    "array", "ast", "atexit", "base64", "binascii", "bisect", "calendar", "decimal", "dis",
    "fractions", "glob", "html", "html.entities", "inspect", "multiprocessing", "pickle",
    "statistics", "string", "sysconfig", "tarfile", "textwrap", "timeit", "tokenize",
]
# 用户安装的扩展（纯 Python 的格式适配器）在子进程里用本包的 Python 运行
# （core/adapter_subprocess_host.py），可能 import 任何标准库模块；PyInstaller 默认只收我们自己
# 用到的，所以把标准库整个收进来。去掉的是图形界面、交互终端、开发与打包工具、测试，以及链接
# GPL 库的 readline / gdbm。冻结冒烟用一个导入常用标准库的适配器在子进程里跑一遍（frozen_smoke.py）。
_STDLIB_EXCLUDED = {
    "tkinter", "_tkinter", "idlelib", "turtle", "turtledemo", "lib2to3", "test", "ensurepip", "venv",
    "distutils", "antigravity", "this", "pydoc", "pydoc_data", "readline", "rlcompleter", "curses",
    "_curses", "_curses_panel", "_gdbm", "dbm", "nis", "ossaudiodev", "spwd", "crypt", "msilib", "winsound",
}
_STDLIB_TEST_PARTS = {"test", "tests", "idle_test"}


def _stdlib_module_wanted(name):
    return not (_STDLIB_TEST_PARTS & set(name.split(".")))


for _name in sorted(sys.stdlib_module_names):
    if _name in _STDLIB_EXCLUDED or _name.startswith(("_test", "__phello__")):
        continue
    hiddenimports.append(_name)
    hiddenimports += collect_submodules(_name, filter=_stdlib_module_wanted, on_error="ignore")
if sys.platform != "win32":
    hiddenimports += ["grp"]
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
        # cryptography (~11MB, dominated by hazmat/bindings/_rust.abi3.so) used to be
        # excluded here: back then it was pulled in solely by xlwings/pro/utils.py's
        # guarded `from cryptography.fernet import Fernet` license check, which we never
        # exercise. Since V9.2.5 core/config_crypto.py imports it for real (X25519 +
        # AES-GCM on exported model configs) and api/app.py imports that at module load,
        # so excluding it now would make the frozen sidecar die on startup. Keep it in.
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
        # The macOS release job installs requirements-test.txt (pytest) before packaging,
        # and two optional imports then drag test tooling in (~1.8MB): httpx's command-line
        # entry point (httpx/_main.py) imports pygments, and anyio's asyncio backend imports
        # _pytest.outcomes in its pytest test-runner helper. Neither runs in the sidecar.
        "pygments", "pytest", "_pytest",
        # pywin32's MFC GUI layer (Pythonwin). Static analysis reaches it through
        # win32com.client.makepy's GUIProgress class, which nothing instantiates; it brings
        # win32ui.pyd plus the 5.6MB mfc140u.dll. Word/Excel automation only needs
        # win32com.client, pythoncom and pywintypes.
        "win32ui", "win32uiole", "pywin", "dde",
        # PyMuPDF（AGPL-3.0，约 60MB）不随安装包分发：它属于「PDF 处理组件」，运行时从用户
        # 连接的 pdf2docx 环境里加载（core/pdf_component.py）。构建机为了跑测试装了它，
        # 这里显式排除，scripts/build_tauri_package.py 打包后还会再查一遍。
        "fitz", "pymupdf",
        # 识别中文线条字的 ONNX Runtime 和它要的 OpenCV 也只在组件里（core/ppocr_component.py
        # 在子进程里加载）；core/ppocr_engine.py 里有 import，构建机装着时不能被顺手收进来。
        "cv2", "onnxruntime",
    ],
    noarchive=False,
    optimize=0,
    # python-docx 新建页眉、页脚、批注、样式、设置部件时按 docx/parts/../templates/… 读模板
    # （docx/parts/hdrftr.py、comments.py 等）。默认收进 PYZ 时磁盘上没有 docx/parts 目录，
    # 这条路径走不通，FileNotFoundError：PDF 转 Word 挪页脚整份失败、Word 公式批注加不上。
    # 以源码文件收 docx，docx/parts 目录就在包里。冻结冒烟会真的建一次页脚和批注。
    module_collection_mode={"docx": "py"},
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
