"""Read-only startup checks for a source tree or frozen executable."""

from __future__ import annotations

import importlib
import sys
from importlib.metadata import PackageNotFoundError, version


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
)

#: Translation types shipped in adapters/ that GET /api/surfaces must list.
_BUNDLED_SURFACES = ("ext:text",)

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
    except Exception as exc:
        _emit(f"[ERROR] Frozen application smoke test failed: {exc}", error=True)
        return 1

    _emit(f"[INFO] Frozen application smoke test passed: {APP_NAME} {APP_VERSION}")
    return 0
