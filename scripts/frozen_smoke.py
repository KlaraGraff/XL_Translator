"""Read-only startup checks for a source tree or frozen executable."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


_CRITICAL_IMPORTS = (
    "app_meta",
    "PIL.Image",
    "fastapi",
    "openpyxl",
    "docx",
    "pypdfium2",
    "httpx",
    "api.launcher",
    # 导出配置的加密要用 cryptography 的原生扩展，而 spec 里一度把它整包排除过。
    # 冻结后少了它，sidecar 会在启动时直接崩，本地开发环境却一切正常——所以这条
    # 必须由冻结冒烟来守。
    "core.config_crypto",
    "core.task_runner",
    "core.word_task_runner",
    "core.pdf_image_translation",
    # 格式适配器的协议包：适配器按文件加载，冻结时漏掉它只会在打开对应页面时才暴露。
    "translator_adapter",
    "core.format_adapter",
    # 一键安装「文字识别组件」靠它解压；它是原生扩展，冻结时漏掉只会在用户点安装时才暴露。
    "zstandard",
    # 中文识别模型的安装和识别子进程的入口（NumPy、ONNX Runtime 只在子进程里从组件加载）。
    "core.ppocr_component",
    "core.ppocr_component_install",
)

#: Translation types shipped in adapters/ that GET /api/surfaces must list.
_BUNDLED_SURFACES = ("ext:text",)

#: The four built-in translation pages; ``types`` must list all of them.
_BUILTIN_TYPES = ("excel", "word", "pdf", "cad")
_TYPES_TIMEOUT_SECONDS = 120

_CRITICAL_DISTRIBUTIONS = (
    "Pillow",
    "fastapi",
    "openpyxl",
    "python-docx",
    "pypdfium2",
)


def _emit(message: str, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    if stream is not None:
        print(message, file=stream)


def _cli_command(*args: str) -> tuple[list[str], str | None]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *args], str(Path(sys.executable).parent)
    root = Path(__file__).resolve().parents[1]
    return [sys.executable, "-m", "cli", *args], str(root)


def _check_cli_types() -> None:
    """Run ``types`` as a user would: the command starts its own engine
    (the sidecar again, frozen) in a throwaway data directory and asks it."""
    with tempfile.TemporaryDirectory(prefix="translator-smoke-") as data_dir:
        command, cwd = _cli_command("types", "--json")
        env = {**os.environ, "TRANSLATOR_APP_DATA_DIR": data_dir}
        env.pop("TRANSLATOR_SIDECAR_PARENT_PID", None)
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TYPES_TIMEOUT_SECONDS,
            check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(f"command line `types` exited {result.returncode}: {result.stderr.strip()[-800:]}")
    keys = {str(item.get("key")) for item in json.loads(result.stdout).get("types") or ()}
    missing = [key for key in _BUILTIN_TYPES if key not in keys]
    if missing:
        raise RuntimeError(f"command line `types` is missing built-in types: {', '.join(missing)}")


def _check_mcp_handshake() -> None:
    """Start ``mcp`` as an MCP client would (stdio, SDK client): initialize,
    list the tools, and call ``list_types`` once."""
    import anyio
    from mcp import Client, StdioServerParameters

    from cli.mcp_server import TOOL_NAMES

    async def talk(data_dir: str) -> None:
        command, cwd = _cli_command("mcp")
        env = {**os.environ, "TRANSLATOR_APP_DATA_DIR": data_dir}
        env.pop("TRANSLATOR_SIDECAR_PARENT_PID", None)
        params = StdioServerParameters(command=command[0], args=command[1:], cwd=cwd, env=env)
        with anyio.fail_after(_TYPES_TIMEOUT_SECONDS):
            async with Client(params) as client:
                names = tuple(tool.name for tool in (await client.list_tools()).tools)
                if sorted(names) != sorted(TOOL_NAMES):
                    raise RuntimeError(f"MCP tools differ: {', '.join(names)}")
                result = await client.call_tool("list_types", {})
                if result.is_error:
                    raise RuntimeError(f"MCP list_types failed: {result.content}")
                keys = {str(item.get("key")) for item in (result.structured_content or {}).get("types") or ()}
                missing = [key for key in _BUILTIN_TYPES if key not in keys]
                if missing:
                    raise RuntimeError(f"MCP list_types is missing built-in types: {', '.join(missing)}")

    with tempfile.TemporaryDirectory(prefix="translator-smoke-") as data_dir:
        anyio.run(talk, data_dir)


#: Standard-library modules a third-party format adapter is likely to use.  The
#: spec collects the whole standard library for them; the check below imports
#: these inside an ``--adapter-host`` child, as an installed adapter would.
_ADAPTER_STDLIB = (
    "csv", "codecs", "configparser", "datetime", "difflib", "email.parser", "gzip", "html.parser",
    "io", "json", "plistlib", "re", "string", "struct", "textwrap", "tomllib", "unicodedata",
    "xml.dom.minidom", "xml.etree.ElementTree", "xml.sax", "zipfile",
)

_SMOKE_ADAPTER = """\
import csv
{imports}
from translator_adapter import OutputFile, TextUnit


def extract(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return [TextUnit(id=f"r{{index}}", text=row[0]) for index, row in enumerate(csv.reader(handle)) if row]


def write(path, units, mode, out_path, options):
    with open(out_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        for unit in units:
            writer.writerow([unit.translation])
    return OutputFile(path=out_path)
"""


def _check_adapter_host() -> None:
    """Run an installed-style adapter in a ``--adapter-host`` child (the sidecar
    again, frozen): extract and write one CSV, importing common standard-library
    modules the way a third-party adapter would."""
    from core.adapter_subprocess_host import SubprocessAdapterHost
    from translator_adapter import MODE_TRANSLATION_ONLY, TranslatedUnit

    with tempfile.TemporaryDirectory(prefix="translator-smoke-adapter-") as folder:
        root = Path(folder)
        adapter = root / "adapter"
        adapter.mkdir()
        manifest = {
            "id": "smoke",
            "version": "1.0.0",
            "adapter_api_version": 1,
            "label": {"zh": "冒烟", "en": "Smoke", "fr": "Fumée"},
            "file_noun": {"zh": "表格", "en": "tables", "fr": "tableaux"},
            "extensions": ["csv"],
        }
        (adapter / "adapter.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        imports = "\n".join(f"import {name}" for name in _ADAPTER_STDLIB)
        (adapter / "adapter.py").write_text(_SMOKE_ADAPTER.format(imports=imports), encoding="utf-8")
        source = root / "table.csv"
        source.write_text("Hello\nWorld\n", encoding="utf-8")
        host = SubprocessAdapterHost(adapter)
        units = host.extract(source)
        if [unit.text for unit in units] != ["Hello", "World"]:
            raise RuntimeError(f"adapter host extract returned {units!r}")
        translated = [TranslatedUnit(id=unit.id, text=unit.text, translation=f"[fr] {unit.text}") for unit in units]
        output = root / "table_fr.csv"
        host.write(source, translated, MODE_TRANSLATION_ONLY, output, {})
        if output.read_text(encoding="utf-8").splitlines() != ["[fr] Hello", "[fr] World"]:
            raise RuntimeError("adapter host write produced unexpected output")


def _check_adapter_cli() -> None:
    """``adapter new`` → ``adapter test`` → ``adapter pack`` on the template, as an author would.

    The template is data in the package (cli/adapter_template, including scripts/pack.py), and
    ``adapter test`` runs the samples in ``--adapter-host`` children."""
    from cli.adapter_commands import check_adapter, new_adapter, pack_adapter

    with tempfile.TemporaryDirectory(prefix="translator-smoke-template-") as folder:
        target = Path(folder) / "smoke-template"
        new_adapter("smoke-template", target)
        report = check_adapter(target)
        if not report.ok or not report.runs:
            raise RuntimeError(f"adapter test failed on the template: {report.problems}")
        result = pack_adapter(target, out=str(Path(folder) / "dist"))
        if not Path(result["zip"]).is_file():
            raise RuntimeError("adapter pack produced no zip")


def run_smoke_test() -> int:
    """Validate bundled imports without creating a QApplication or reading settings."""
    try:
        for module_name in _CRITICAL_IMPORTS:
            importlib.import_module(module_name)

        from app_meta import APP_BUNDLE_IDENTIFIER, APP_NAME, APP_VERSION

        if not APP_NAME.strip() or not APP_VERSION.strip() or not APP_BUNDLE_IDENTIFIER.strip():
            raise RuntimeError("application metadata is incomplete")

        for distribution_name in _CRITICAL_DISTRIBUTIONS:
            try:
                installed_version = version(distribution_name)
            except PackageNotFoundError as exc:
                raise RuntimeError(
                    f"required distribution metadata is missing: {distribution_name}"
                ) from exc
            if not installed_version.strip():
                raise RuntimeError(
                    f"required distribution has no version: {distribution_name}"
                )
        # 原位替换的词表是数据文件，漏收时 import 照样成功，只会悄悄把识别结果都判成「查不到」。
        from core.pdf_vector.classify import lexicon

        for lang in ("fr", "en"):
            if not lexicon(lang):
                raise RuntimeError(f"in-place word list is missing: {lang}")

        # 随应用提供的翻译类型是数据文件（adapters/<id>/adapter.json + adapter.py），漏收时
        # 应用照常启动，只是 GET /api/surfaces 里少了它们、侧栏少了对应页面。
        from api.surfaces import all_surfaces
        from api.surfaces.adapter import register_bundled_adapters

        register_bundled_adapters()
        registered = {surface.key: surface for surface in all_surfaces()}
        for key in _BUNDLED_SURFACES:
            if key not in registered:
                raise RuntimeError(f"bundled translation type is missing: {key}")
            registered[key].host.load()

        # 命令行入口：冻结包里起引擎、列出类型，漏收 cli 包或分派出错在这里就暴露。
        _check_cli_types()
        # MCP 入口：握手、列出七个工具、调一次 list_types（打包时漏收 mcp 的依赖在这里暴露）。
        _check_mcp_handshake()
        # 用户安装的扩展在 --adapter-host 子进程里跑；漏收标准库或分派出错在这里暴露。
        _check_adapter_host()
        # 制作扩展的命令：模板是数据文件，漏收时 adapter new / pack 才会出错。
        _check_adapter_cli()
    except Exception as exc:
        _emit(f"[ERROR] Frozen application smoke test failed: {exc}", error=True)
        return 1

    _emit(f"[INFO] Frozen application smoke test passed: {APP_NAME} {APP_VERSION}")
    return 0
